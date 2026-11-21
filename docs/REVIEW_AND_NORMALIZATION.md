# CueCarry review and optional loudness matching plan

CueCarry already has a thoughtful foundation: the engine is separate from both
interfaces, previews share the conversion plan, originals are intended to stay
untouched, and the tests check actual audio timing. Those are especially helpful
choices for a tool people will trust with their music libraries. The suggestions
below build on that work and keep the first change small.

## Changes in this pass

No confirmed P0 issue was found in this source review. Here P0 means an immediate,
widespread blocker requiring an emergency release. The file-loss edge cases below
are P1 safety fixes worth landing before more audio processing is added.

| Area | Change and reason |
|---|---|
| Output collisions | Reject plans where two inputs, such as `track.flac` and `track.wav`, map to one MP3. Previously concurrent workers could share a `.part` file, and later runs could silently reuse the wrong audio. Case differences are checked too. |
| Protect originals | Check planned MP3s, temporary audio files, output XML, and failure reports against source track paths and the input XML. A custom XML location must not overwrite music or another planned output. |
| Folder checks | Resolve relative paths, `..`, and existing directory links when comparing locations. A path that leaves the source folder is skipped; an output folder resolving inside it is rejected. |
| XML exports | Write to a unique temporary file beside the destination and replace the export only after serialization succeeds. Failed writes preserve the previous export and clean up the temporary XML. |
| Stop | Remember Stop before the GUI worker creates its runner, and register new encoder processes under the same lock used to stop them. This closes two startup races. |
| CLI feedback | Missing, malformed, and non-collection XML inputs return a short error and exit code 2. Invalid worker counts, negative trim counts, and unknown timing modes are rejected during planning. |

The collision policy deliberately asks for distinct filenames instead of inventing
new names. This keeps previously exported paths predictable. These checks address
accidental conflicts in the loaded library; they are not a filesystem security
boundary against hard links, concurrent app instances, or paths changed during a run.

`tests/test_safety.py` covers planning and export failures without Qt or FFmpeg.
The GUI tests also cover Stop arriving before worker startup.

Validation on Windows with Python 3.14.4, PySide6 Essentials 6.12.0, pytest 9.2.0,
and a local test FFmpeg: **30 tests passed**, including real-audio conversion,
cue timing, and GUI flows. Python compilation and `git diff --check` also passed.
The packaged Windows release and physical DJ hardware still need their own checks.

## Suggested follow-ups

These would make good small, separate contributions. Priorities describe impact,
not criticism of the existing work.

| Priority | Opportunity | What it takes |
|---|---|---|
| P1 before normalization | Verify existing outputs before resuming | `Plan.todo` and `write_xml` currently trust file existence. Store source identity, encoder version, timing mode, bitrate, actual sample rate, and later normalization settings in a versioned manifest. Publish it safely with each completed output. Rebuild or clearly flag mismatches; support existing exports explicitly. Failed overwrites must not publish old files with new settings. |
| P1 | Distinguish AAC from an unreadable probe | `audio_codec` returns an empty string on failure, while the runner treats any non-ALAC result as AAC. Return a structured probe result, preserve only confirmed AAC, and report unknown/corrupt/unsupported streams as failures. Give the XML writer the same result so failed probes cannot reappear as successful imports. |
| P1 | Base timing on actual audio properties | XML sample rates may be missing or stale. Probe the audio stream once, explicitly choose a supported MP3 sample rate, and share that value between the encoder and cue shifting. Cover low rates and 88.2/96 kHz sources. Align currently trims input samples while cue compensation uses output samples; test Align at high rates before promising equivalent timing. |
| P1 for distributed builds | Complete the release source and license bundle | Keep the exact dependency source, notices, build configuration, and source access with each binary release; details below. |
| P2 | Make CLI cancellation prompt | `ThreadPoolExecutor` waits during exception unwinding, which can delay the outer Ctrl+C handler. Catch interruption inside the executor lifetime, kill registered processes, cancel pending work, and test with a deliberately blocked encoder. |
| P2 | Finish shutdown and temporary-file handling | GUI close waits at most ten seconds, while probing can take sixty. Make probes cancellable, keep the window alive until cleanup finishes, and clean `.part` files after exceptional paths. Use unique per-run audio temporary files or an output lock for concurrent runs. |
| P2 | Disambiguate playlist names | Selection is keyed by leaf name, so two folders containing a playlist called `Warmup` select both. Use a stable tree identity or full playlist path in the engine and GUI, and define a CLI syntax that preserves existing simple names. |
| P2 | Improve collection validation | Reject missing/duplicate TrackIDs and invalid numeric cue/rate fields with track-specific messages. Avoid partial results from invalid metadata. |
| P2 | Cover path and test portability | Add drive-root, UNC share, linked-folder, and relative-root cases. Root detection currently reduces a Windows drive root to `J:`, which is drive-relative. Require explicit output selection when there is no sensible sibling folder. Move pure path tests out of the module-wide FFmpeg skip; skip GUI audio tests explicitly when FFmpeg is absent. |
| P2 | Make builds reproducible | Pin the FFmpeg asset and checksum, record `-version`/`-buildconf`/`-L`, use a unique extraction folder, and verify the license of cached binaries too. Guard each native build command's exit status. Add Windows CI for tests and a packaged-app smoke check. |

## Proposed normalization behavior

Add **Match track loudness**, off by default. The goal is fewer trim adjustments
when moving between tracks, while preserving the dynamics within each track.
Peak normalization alone would not meet that goal: tracks with similar peaks can
still sound quite different in loudness.

For the first version, normalize only the lossless tracks CueCarry already
converts. Apply one constant gain during the existing MP3 encode, with no extra
lossy generation. Leave originals untouched. Explain that existing MP3/AAC tracks
remain unchanged, so a mixed collection will not be fully matched. An eventual
option for those formats needs an explicit quality tradeoff and separate copies.

Suggested prototype controls are a target of **-14 LUFS** and a true-peak ceiling
of **-1.5 dBTP**. These are listening-test starting points, not a DJ standard or a
guarantee of equal perceived loudness. Audition several genres and club masters
before choosing defaults. Mixer EQ, track arrangement, and player auto-gain still
affect the result; compare with auto-gain both disabled and enabled to understand
how the chosen playback setup behaves.

### Measurement and gain

Use the existing FFmpeg executable for a full-track analysis, measuring integrated
loudness in LUFS and true peak in dBTP. Its `ebur128` and `loudnorm` filters provide
the underlying measurements. `loudnorm` can fall back from linear to dynamic
processing, so requesting linear mode alone does not ensure preserved dynamics.
See the [FFmpeg filter documentation](https://ffmpeg.org/ffmpeg-filters.html#loudnorm).

For a constant-gain implementation, calculate:

```text
wanted gain dB = target LUFS - measured integrated LUFS
allowed gain dB = peak ceiling dBTP - measured true peak dBTP
applied gain dB = min(wanted gain dB, allowed gain dB, maximum boost dB)
```

Start with a proposed maximum boost of 6 dB and show when the peak ceiling or
boost cap prevents reaching the target. Reject non-finite measurements and leave
silent or too-short unmeasurable material unchanged with an explanation. Apply
the chosen gain using a `volume` filter, with no compressor, limiter, or automatic
gain changes during the track. For Align, measure the same trimmed/resampled
signal that will be encoded. Compose filters into one chain so normalization
does not replace the existing timing filter.

MP3 encoding can create new peaks. Decode and measure the completed MP3 before
publishing it; if it exceeds the ceiling, reduce gain and encode again from the
original lossless source. Bound retries and report failures honestly. Measure
achieved loudness too, since gating can affect the simple predicted result.

### Implementation steps and effort

1. **Establish output identity and probing first.** Implement the manifest and
   shared stream properties described above. Tests must prove that a normal
   export cannot be silently reused as a normalized export or vice versa.
2. **Add an analysis module.** A small `app/loudness.py` can own measurements,
   finite-value validation, gain decisions, and cache records. Include source
   size/mtime and a robust identity policy, analysis version, FFmpeg build, sample
   rate, channels, and preprocessing in cache keys. Silence and probe errors need
   distinct results. Analysis processes must participate in Runner cancellation.
3. **Integrate the encoder.** Extend Settings with an enabled flag, target,
   ceiling, and boost cap. Share the planned sample rate with analysis and output.
   Keep gain and timing separate, publish only verified audio, and record applied
   gain, measured results, and the exact processing options in the manifest.
4. **Expose the option.** Add a checkbox with a brief explanation and persist it
   through QSettings. Proposed CLI flags are `--normalize`, `--target-lufs`, and
   `--true-peak`. Regular Preview should stay fast; offer explicit analysis with
   progress and Stop. Show measured loudness, proposed gain, and any peak limit.
   XML-only mode must describe existing audio faithfully and reject requests to
   apply new normalization settings without encoding.
5. **Verify and audition.** Cover loud/quiet copies of the same signal, silence,
   transients, mono/stereo, corrupt probes, cancellation, resume, setting changes,
   and all timing modes/rates. Assert peak limits and gain, unchanged originals,
   no new cue drift, preserved tags/art/playlists, and unchanged behavior with
   the option off. Check a representative playlist in rekordbox and intended
   hardware, including its auto-gain behavior.

A rough planning allowance for someone familiar with the repo is **5–9 working
days**: 1–2 for probing and resume identity, 2–3 for analysis/encoding/verification,
1–2 for UI and regression coverage, and 1–2 for listening and release checks.
That assumes existing FFmpeg filters are available and excludes normalization of
already-lossy files. Hardware testing and target selection may take longer.

## License and sharing

Keep CueCarry under **GPL-3.0-or-later**, preserve its SPDX and contributor notices,
and identify modified releases so the original author is not credited with changes
they did not make. Sharing a modified executable should include access to its
matching source and build scripts under the GPL. Private modifications do not
require public posting. See the [GNU GPL FAQ](https://www.gnu.org/licenses/gpl-faq.html)
and the repository's [LICENSE](../LICENSE).

The existing separate FFmpeg executable and replaceable Qt libraries are useful
choices. Normalization can use FFmpeg filters without introducing a new runtime
dependency. Keep the intended LGPL FFmpeg build and check its actual configuration;
the project license does not make a nonfree FFmpeg build redistributable. FFmpeg's
[license guidance](https://ffmpeg.org/legal.html) explains that optional components
can change its license and emphasizes matching source and build information.

Before distributing another binary, extend the current notices with the exact
FFmpeg/LAME and Qt/PySide/Shiboken source versions and required license texts,
Python notices, and applicable PyInstaller notices. Archive matching dependency
source and build instructions alongside the app's corresponding source, with a
clear download route from the release. The current generic upstream source links
and moving `latest` FFmpeg download are not sufficient evidence that a recipient
can obtain the exact sources used. Verify the final zip contents and dependency
terms for that release; keep the existing rekordbox trademark attribution.

This pass changes no licensing terms and adds no runtime dependencies. The test
FFmpeg obtained through `imageio-ffmpeg` is only a local verification tool and must
not be substituted for the project's intended release binary without checking
its build and license.
