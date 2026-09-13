import android.content.ComponentName;
import android.content.ContextWrapper;
import android.media.MediaMetadata;
import android.media.session.MediaController;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.net.Uri;
import android.os.Bundle;
import android.os.IBinder;
import android.os.IInterface;

import java.lang.reflect.Method;
import java.util.List;

/**
 * Drives Apple Music's MediaSession from the adb shell uid, which holds
 * MEDIA_CONTENT_CONTROL — so no APK, root, or UI taps are needed.
 *
 * Run: CLASSPATH=/data/local/tmp/musicbot-mediactl.dex app_process / MediaCtl <cmd> [arg]
 *   now | list | mediaid <storeId> | search <query> | uri <uri> | next | previous | playpause
 *
 * Output is tab-separated for the bot to parse:
 *   BEFORE  state artist title   (always)
 *   AFTER   state artist title   (the command took effect)
 *   TIMEOUT state artist title   (nothing changed within WAIT_MS)
 */
public class MediaCtl {
    static final String PKG = "com.apple.android.music";
    static final long WAIT_MS = 10_000;

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

    static String clean(String s) {
        return s == null ? "" : s.replace('\t', ' ').replace('\n', ' ');
    }

    static String track(MediaController c) {
        MediaMetadata md = c.getMetadata();
        if (md == null) return "\t";
        return clean(md.getString(MediaMetadata.METADATA_KEY_ARTIST)) + "\t"
                + clean(md.getString(MediaMetadata.METADATA_KEY_TITLE));
    }

    static void print(String tag, MediaController c) {
        System.out.println(tag + "\t" + stateName(c) + "\t" + track(c));
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
        boolean wasPlaying = stateName(c).equals("playing");

        MediaController.TransportControls tc = c.getTransportControls();
        boolean waitForTrack = true;
        switch (cmd) {
            case "now": return;
            case "mediaid": tc.playFromMediaId(arg, new Bundle()); break;
            case "search": tc.playFromSearch(arg, new Bundle()); break;
            case "uri": tc.playFromUri(Uri.parse(arg), new Bundle()); break;
            case "next": tc.skipToNext(); break;
            case "previous": tc.skipToPrevious(); break;
            case "playpause":
                if (wasPlaying) tc.pause(); else tc.play();
                waitForTrack = false;
                break;
            default: throw new IllegalArgumentException("unknown command: " + cmd);
        }

        long deadline = System.currentTimeMillis() + WAIT_MS;
        while (System.currentTimeMillis() < deadline) {
            Thread.sleep(250);
            boolean changed = waitForTrack
                    ? !track(c).equals(beforeTrack)
                    : stateName(c).equals("playing") != wasPlaying;
            if (changed) {
                print("AFTER", c);
                return;
            }
        }
        print("TIMEOUT", c);
    }
}
