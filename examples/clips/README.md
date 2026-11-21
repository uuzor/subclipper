# Example rendered clips

Sample output from an end-to-end run of the pipeline: upload → transcribe →
analyze → detect moments → score → render.

Source media is a short synthetic interview generated locally with
`espeak-ng` and `ffmpeg` (no third-party footage). It exists only to exercise
the real providers (faster-whisper, Gemini vision, OpenRouter moment
detection) and the ffmpeg renderer.

| File | Rank | Score | Window | Type |
| --- | --- | --- | --- | --- |
| `clip1-biggest-mistake-9x16.mp4` | 1 | 42.74 | 5.3s–20.9s | personal_failure |
| `clip2-obsession-over-intelligence-9x16.mp4` | 2 | 30.72 | 17.3s–35.4s | counterintuitive_insight |
| `clip3-never-about-the-idea-9x16.mp4` | 3 | 26.43 | 35.4s–46.6s | controversial_opinion |

All clips are 1080×1920 H.264/AAC with burned-in captions, produced by
`POST /api/v1/projects/{id}/render`.

Note: scores are model predictions, not guarantees, and the synthetic audio
does not represent production quality.
