// Request the newest relay playback instead of the app's 3000 ms cache.
// This setting is experimental: it does not establish camera capture latency.
export const DEFAULT_PLAYBACK_CACHE_MS = 0;

// Keep explicit compatibility settings within the previously used app cache.
// This is a local input limit, not a claim about the relay's supported range.
export function validPlaybackCacheMs(value) {
  return Number.isInteger(value) && value >= 0 && value <= 3000;
}
