import android.content.ComponentName;
import android.content.ContextWrapper;
import android.media.MediaDescription;
import android.media.MediaMetadata;
import android.media.session.MediaController;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.net.Uri;
import android.os.Bundle;
import android.os.IBinder;
import android.os.IInterface;

import com.apple.android.music.playback.queue.StorePlaybackQueueItemProvider;

import java.lang.reflect.Method;
import java.util.Arrays;
import java.util.List;

/**
 * Drives Apple Music's MediaSession from the adb shell uid, which holds
 * MEDIA_CONTENT_CONTROL — so no APK, root, or UI taps are needed.
 *
 * Run: CLASSPATH=/data/local/tmp/musicbot-mediactl.dex app_process / MediaCtl <cmd> [args]
 *   now | list | queue | mediaid <storeId> | search <query> | uri <uri>
 *   next | previous | playpause | enqueue <insertionType|end> <storeId>... | remove <itemQueueId>
 *   play <insertionType> <storeId>...   (PLAY_PROVIDER; 6 = keep Playing Next, 5 = clear it)
 *
 * Output is tab-separated for the bot to parse:
 *   BEFORE  state artist title   (always)
 *   AFTER   state artist title   (the command took effect)
 *   TIMEOUT state artist title   (nothing changed within WAIT_MS)
 *   ITEM    active mediaId title artist inQueueSection fromAutoplay itemQueueId
 *           (queue/enqueue/remove: the visible queue; flags are 0/1)
 */
public class MediaCtl {
    static final String PKG = "com.apple.android.music";
    static final long WAIT_MS = 10_000;

    // Apple Music's own custom session command (handled in its media3 callback's
    // onCustomCommand → MediaPlayerController.addQueueItems). Insertion types are
    // PlaybackQueueInsertionType: 3 = AFTER_CURRENT_ITEM, 10 = AT_END_OF_QUEUE_SECTION
    // (2 = AT_END appends after Autoplay).
    static final String ADD_QUEUE_ITEMS = "com.apple.android.music.playback.command.ADD_QUEUE_ITEMS";
    static final String ARG_PROVIDER = "com.apple.android.music.playback.command.ARGUMENT_PLAYBACK_QUEUE_ITEM_PROVIDER";
    static final String ARG_INSERTION_TYPE = "com.apple.android.music.playback.command.ARGUMENT_PLAYBACK_QUEUE_INSERTION_TYPE";
    static final int INSERT_AFTER_CURRENT_ITEM = 3;
    static final int INSERT_AT_END_OF_QUEUE_SECTION = 10;
    static final String EXTRA_CAN_ADD_TO_QUEUE_SECTION = "com.apple.android.music.playback.playbackstate.EXTRA_CAN_ADD_TO_QUEUE_SECTION";
    static final String PLAY_PROVIDER = "com.apple.android.music.playback.action.PLAY_PROVIDER";
    static final String ARG_PLAY_PROVIDER = "com.apple.android.music.playback.action.ARGUMENT_PLAYBACK_QUEUE_ITEM_PROVIDER";
    static final String ARG_PLAY_INSERTION_TYPE = "com.apple.android.music.playback.action.ARGUMENT_PLAYBACK_QUEUE_INSERTION_TYPE";
    static final String REMOVE_QUEUE_ITEM = "com.apple.android.music.playback.command.REMOVE_QUEUE_ITEM";
    static final String ARG_QUEUE_ID = "com.apple.android.music.playback.command.ARGUMENT_PLAYBACK_QUEUE_ID";

    // Per-item flags Apple Music puts in each QueueItem's description extras:
    // in the user's "Playing Next" section, or appended by Autoplay.
    static final String META_IN_QUEUE_SECTION = "com.apple.android.music.playback.metadata.METADATA_KEY_IS_IN_QUEUE_SECTION";
    static final String META_FROM_AUTOPLAY = "com.apple.android.music.playback.metadata.METADATA_KEY_IS_FROM_CONTINUOUS_PLAYBACK";
    static final String META_ITEM_QUEUE_ID = "com.apple.android.music.playback.metadata.ITEM_QUEUE_ID";

    enum Wait { TRACK, PLAY_STATE, QUEUE }

    // MediaController only needs a context for getPackageName() on transport calls.
    static class ShellContext extends ContextWrapper {
        ShellContext() { super(null); }
        @Override public String getPackageName() { return "com.android.shell"; }
        @Override public String getOpPackageName() { return "com.android.shell"; }
    }

    @SuppressWarnings("unchecked")
    static List<MediaSession.Token> sessions() throws Exception {
        Class<?> sm = Class.forName("android.os.ServiceManager");
        IBinder b = (IBinder) sm.getMethod("getService", String.class).invoke(null, "media_session");
        Class<?> stub = Class.forName("android.media.session.ISessionManager$Stub");
        IInterface msm = (IInterface) stub.getMethod("asInterface", IBinder.class).invoke(null, b);
        Method m = msm.getClass().getMethod("getSessions", ComponentName.class, int.class);
        return (List<MediaSession.Token>) m.invoke(msm, null, 0);
    }

    static MediaController find(ShellContext ctx) throws Exception {
        for (MediaSession.Token t : sessions()) {
            MediaController c = new MediaController(ctx, t);
            if (PKG.equals(c.getPackageName())) return c;
        }
        throw new IllegalStateException("no " + PKG + " media session (is Apple Music running?)");
    }

    static String stateName(MediaController c) {
        PlaybackState s = c.getPlaybackState();
        if (s == null) return "none";
        switch (s.getState()) {
            case PlaybackState.STATE_PLAYING: return "playing";
            case PlaybackState.STATE_PAUSED: return "paused";
            case PlaybackState.STATE_STOPPED: return "stopped";
            case PlaybackState.STATE_BUFFERING: return "buffering";
            case PlaybackState.STATE_CONNECTING: return "connecting";
            case PlaybackState.STATE_ERROR: return "error";
            case PlaybackState.STATE_SKIPPING_TO_NEXT: return "skipping";
            default: return "other";
        }
    }

    static String clean(CharSequence s) {
        return s == null ? "" : s.toString().replace('\t', ' ').replace('\n', ' ');
    }

    static String track(MediaController c) {
        MediaMetadata md = c.getMetadata();
        if (md == null) return "\t";
        return clean(md.getString(MediaMetadata.METADATA_KEY_ARTIST)) + "\t"
                + clean(md.getString(MediaMetadata.METADATA_KEY_TITLE));
    }

    static String queueKey(MediaController c) {
        List<MediaSession.QueueItem> q = c.getQueue();
        if (q == null) return "";
        StringBuilder sb = new StringBuilder();
        for (MediaSession.QueueItem it : q) sb.append(it.getDescription().getMediaId()).append(',');
        return sb.toString();
    }

    /**
     * "end" = end of the user's Playing Next section. The app ignores
     * AT_END_OF_QUEUE_SECTION while that section is empty (it reports
     * EXTRA_CAN_ADD_TO_QUEUE_SECTION=false), and then "after the current song"
     * is the same spot. Anything else is a literal insertion type.
     */
    static int insertionType(MediaController c, String arg) {
        if (!arg.equals("end")) return Integer.parseInt(arg);
        Bundle ex = c.getExtras();
        boolean canAddToSection = ex != null && ex.getBoolean(EXTRA_CAN_ADD_TO_QUEUE_SECTION);
        return canAddToSection ? INSERT_AT_END_OF_QUEUE_SECTION : INSERT_AFTER_CURRENT_ITEM;
    }

    static void print(String tag, MediaController c) {
        System.out.println(tag + "\t" + stateName(c) + "\t" + track(c));
    }

    static void printQueue(MediaController c) {
        List<MediaSession.QueueItem> q = c.getQueue();
        if (q == null) return;
        PlaybackState s = c.getPlaybackState();
        long active = s == null ? -1 : s.getActiveQueueItemId();
        for (MediaSession.QueueItem it : q) {
            MediaDescription d = it.getDescription();
            Bundle ex = d.getExtras();
            boolean section = ex != null && ex.getBoolean(META_IN_QUEUE_SECTION);
            boolean autoplay = ex != null && ex.getBoolean(META_FROM_AUTOPLAY);
            long queueId = ex == null ? -1 : ex.getLong(META_ITEM_QUEUE_ID, -1);
            System.out.println("ITEM\t" + (it.getQueueId() == active ? "1" : "0") + "\t" + clean(d.getMediaId())
                    + "\t" + clean(d.getTitle()) + "\t" + clean(d.getSubtitle())
                    + "\t" + (section ? "1" : "0") + "\t" + (autoplay ? "1" : "0") + "\t" + queueId);
        }
    }

    public static void main(String[] args) throws Exception {
        ShellContext ctx = new ShellContext();
        String cmd = args.length > 0 ? args[0] : "now";
        String arg = args.length > 1 ? args[1] : "";

        if (cmd.equals("list")) {
            for (MediaSession.Token t : sessions()) {
                MediaController c = new MediaController(ctx, t);
                System.out.println(c.getPackageName() + "\t" + stateName(c) + "\t" + track(c));
            }
            return;
        }

        MediaController c = find(ctx);
        print("BEFORE", c);
        String beforeTrack = track(c);
        String beforeQueue = queueKey(c);
        boolean wasPlaying = stateName(c).equals("playing");

        MediaController.TransportControls tc = c.getTransportControls();
        Wait wait = Wait.TRACK;
        switch (cmd) {
            case "now": return;
            case "queue": printQueue(c); return;
            case "mediaid": tc.playFromMediaId(arg, new Bundle()); break;
            case "search": tc.playFromSearch(arg, new Bundle()); break;
            case "uri": tc.playFromUri(Uri.parse(arg), new Bundle()); break;
            case "next": tc.skipToNext(); break;
            case "previous": tc.skipToPrevious(); break;
            case "playpause":
                if (wasPlaying) tc.pause(); else tc.play();
                wait = Wait.PLAY_STATE;
                break;
            case "enqueue": {
                String[] ids = Arrays.copyOfRange(args, 2, args.length);
                Bundle b = new Bundle();
                b.putParcelable(ARG_PROVIDER, new StorePlaybackQueueItemProvider(ids));
                b.putInt(ARG_INSERTION_TYPE, insertionType(c, arg));
                c.sendCommand(ADD_QUEUE_ITEMS, b, null);
                wait = Wait.QUEUE;
                break;
            }
            case "play": {
                // Apple Music's PLAY_PROVIDER action: start these songs with an explicit
                // insertion type. KEEP_AND_REPLACE (6) / CLEAR_AND_REPLACE (5) are the
                // app's own answers to the "keep or clear your queued songs?" prompt
                // that playFromMediaId (plain REPLACE) triggers.
                String[] ids = Arrays.copyOfRange(args, 2, args.length);
                Bundle b = new Bundle();
                b.putParcelable(ARG_PLAY_PROVIDER, new StorePlaybackQueueItemProvider(ids));
                b.putInt(ARG_PLAY_INSERTION_TYPE, Integer.parseInt(arg));
                c.sendCommand(PLAY_PROVIDER, b, null);
                break;
            }
            case "remove": {
                Bundle b = new Bundle();
                b.putLong(ARG_QUEUE_ID, Long.parseLong(arg));
                c.sendCommand(REMOVE_QUEUE_ITEM, b, null);
                wait = Wait.QUEUE;
                break;
            }
            default: throw new IllegalArgumentException("unknown command: " + cmd);
        }

        long deadline = System.currentTimeMillis() + WAIT_MS;
        while (System.currentTimeMillis() < deadline) {
            Thread.sleep(250);
            boolean changed;
            switch (wait) {
                case PLAY_STATE: changed = stateName(c).equals("playing") != wasPlaying; break;
                case QUEUE: changed = !queueKey(c).equals(beforeQueue); break;
                default: changed = !track(c).equals(beforeTrack);
            }
            if (changed) {
                print("AFTER", c);
                if (wait == Wait.QUEUE) printQueue(c);
                return;
            }
        }
        print("TIMEOUT", c);
        if (wait == Wait.QUEUE) printQueue(c);
    }
}
