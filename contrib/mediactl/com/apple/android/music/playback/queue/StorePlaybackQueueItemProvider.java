package com.apple.android.music.playback.queue;

import android.os.Parcel;
import android.os.Parcelable;

/**
 * Write-only stand-in carrying Apple Music's class name, so the app unparcels it
 * as its own StorePlaybackQueueItemProvider (a list of store song IDs). Field
 * order mirrors BasePlaybackQueueItemProvider.writeToParcel followed by
 * StorePlaybackQueueItemProvider.writeToParcel in the app.
 */
public class StorePlaybackQueueItemProvider implements Parcelable {
    final String[] ids;

    public StorePlaybackQueueItemProvider(String[] ids) { this.ids = ids; }

    @Override public int describeContents() { return 0; }

    // Values match BasePlaybackQueueItemProvider's no-arg constructor defaults.
    @Override public void writeToParcel(Parcel p, int flags) {
        p.writeInt(-1);       // id: unassigned. A real id (even 0) makes the queue
                              // manager reuse that existing provider's items.
        p.writeInt(-1);       // startItemIndex
        p.writeInt(0);        // shuffleMode
        p.writeInt(-1);       // globalShuffleMode
        p.writeString(null);  // playActivityFeatureName
        p.writeString(null);  // recommendationId
        p.writeString(null);  // playlistVersionHash
        p.writeInt(0);        // isMirroringRemoteQueue
        p.writeString(null);  // containerTitle
        p.writeInt(0);        // removedTracksCount
        p.writeInt(ids.length);
        p.writeStringArray(ids);
    }

    public static final Creator<StorePlaybackQueueItemProvider> CREATOR = new Creator<StorePlaybackQueueItemProvider>() {
        @Override public StorePlaybackQueueItemProvider createFromParcel(Parcel in) { throw new UnsupportedOperationException(); }
        @Override public StorePlaybackQueueItemProvider[] newArray(int n) { return new StorePlaybackQueueItemProvider[n]; }
    };
}
