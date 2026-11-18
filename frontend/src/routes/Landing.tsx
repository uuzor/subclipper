import { Link } from "react-router-dom";
import { Timeline } from "../components/Timeline";
import { ScoreBreakdown } from "../components/ScoreBreakdown";
import { IconArrow, IconScissors, IconWarn } from "../components/icons";
import type { Clip } from "../lib/types";
import "./landing.css";

/* ---- demo data ---------------------------------------------------------
   Synthetic, authored at full fidelity, and labelled as such. These are not
   real analysis results; they exist so the mechanism is visible before anyone
   uploads anything. */
const DEMO_DURATION = 2 * 3600 + 12 * 60 + 34; // 2:12:34

const DEMO_CLIPS: Clip[] = [
  {
    id: "d1",
    project_id: "demo",
    start_seconds: 4346,
    end_seconds: 4398,
    raw_start_seconds: 4354,
    raw_end_seconds: 4392,
    duration_seconds: 52,
    title: 'The biggest mistake I made',
    hook: 'One sentence that reframes his whole career',
    transcript_excerpt: 'The biggest mistake I ever made was trusting everyone around me in my first company. I gave away equity to people who quit inside six months.',
    moment_type: "personal_failure",
    rationale: 'A specific, costly admission delivered as a single sentence. It pays off without the surrounding episode.',
    score: 90.65,
    score_components: {
      emotional_intensity: 0.94,
      hook_strength: 0.97,
      storytelling: 0.88,
      shareability: 0.9,
      audio_visual_engagement: 0.71,
      standalone_context: 0.95,
    },
    score_breakdown: {
      base_score: 90.65,
      penalty_total: 0,
      penalties: [],
      components: [
        { key: "emotional_intensity", label: 'Emotional intensity', weight: 0.2, value: 0.94, contribution: 18.8, explainer: 'Emotional language and reactive audio (laughter, emphasis).' },
        { key: "hook_strength", label: 'Hook strength', weight: 0.2, value: 0.97, contribution: 19.4, explainer: 'Does the first 2-3 seconds capture attention?' },
        { key: "storytelling", label: 'Storytelling / curiosity', weight: 0.2, value: 0.88, contribution: 17.6, explainer: 'Narrative shape: setup, turn and payoff.' },
        { key: "shareability", label: 'Shareability', weight: 0.15, value: 0.9, contribution: 13.5, explainer: 'Quotability, length fit and cleanliness of delivery.' },
        { key: "audio_visual_engagement", label: 'Audio & visual engagement', weight: 0.1, value: 0.71, contribution: 7.1, explainer: 'Loudness peaks, scene changes and visual highlights.' },
        { key: "standalone_context", label: 'Standalone context', weight: 0.15, value: 0.95, contribution: 14.25, explainer: 'Does the clip make sense without the full video?' },
      ],
      weights: { "emotional_intensity": 0.2, "hook_strength": 0.2, "storytelling": 0.2, "shareability": 0.15, "audio_visual_engagement": 0.1, "standalone_context": 0.15 },
    },
    rank: 1,
    status: "candidate",
    render_path: null,
    render_meta: {},
  },
  {
    id: "d2",
    project_id: "demo",
    start_seconds: 2048,
    end_seconds: 2105,
    raw_start_seconds: 2052,
    raw_end_seconds: 2100,
    duration_seconds: 57,
    title: "Degrees don't guarantee it",
    hook: 'A thesis a viewer can argue with in the comments',
    transcript_excerpt: 'University degrees do not guarantee success. I have hired hundreds of people and the degree told me almost nothing.',
    moment_type: "controversial_opinion",
    rationale: 'A contestable claim backed by first-hand evidence. Comment sections reward disagreement.',
    score: 80.2,
    score_components: {
      emotional_intensity: 0.66,
      hook_strength: 0.91,
      storytelling: 0.72,
      shareability: 0.96,
      audio_visual_engagement: 0.68,
      standalone_context: 0.88,
    },
    score_breakdown: {
      base_score: 80.2,
      penalty_total: 0,
      penalties: [],
      components: [
        { key: "emotional_intensity", label: 'Emotional intensity', weight: 0.2, value: 0.66, contribution: 13.2, explainer: 'Emotional language and reactive audio (laughter, emphasis).' },
        { key: "hook_strength", label: 'Hook strength', weight: 0.2, value: 0.91, contribution: 18.2, explainer: 'Does the first 2-3 seconds capture attention?' },
        { key: "storytelling", label: 'Storytelling / curiosity', weight: 0.2, value: 0.72, contribution: 14.4, explainer: 'Narrative shape: setup, turn and payoff.' },
        { key: "shareability", label: 'Shareability', weight: 0.15, value: 0.96, contribution: 14.4, explainer: 'Quotability, length fit and cleanliness of delivery.' },
        { key: "audio_visual_engagement", label: 'Audio & visual engagement', weight: 0.1, value: 0.68, contribution: 6.8, explainer: 'Loudness peaks, scene changes and visual highlights.' },
        { key: "standalone_context", label: 'Standalone context', weight: 0.15, value: 0.88, contribution: 13.2, explainer: 'Does the clip make sense without the full video?' },
      ],
      weights: { "emotional_intensity": 0.2, "hook_strength": 0.2, "storytelling": 0.2, "shareability": 0.15, "audio_visual_engagement": 0.1, "standalone_context": 0.15 },
    },
    rank: 2,
    status: "candidate",
    render_path: null,
    render_meta: {},
  },
  {
    id: "d3",
    project_id: "demo",
    start_seconds: 2900,
    end_seconds: 2944,
    raw_start_seconds: 2902,
    raw_end_seconds: 2942,
    duration_seconds: 44,
    title: 'The room turns',
    hook: 'A laugh that carries the clip on its own',
    transcript_excerpt: 'Wait, you actually wrote that down? — I did, and I still think it was a good idea.',
    moment_type: "funny_reaction",
    rationale: 'Laughter lands at 48:26 and the reply completes the beat. The clip ends on the reaction, not after it.',
    score: 78.55,
    score_components: {
      emotional_intensity: 0.83,
      hook_strength: 0.79,
      storytelling: 0.61,
      shareability: 0.93,
      audio_visual_engagement: 0.95,
      standalone_context: 0.7,
    },
    score_breakdown: {
      base_score: 78.55,
      penalty_total: 0,
      penalties: [],
      components: [
        { key: "emotional_intensity", label: 'Emotional intensity', weight: 0.2, value: 0.83, contribution: 16.6, explainer: 'Emotional language and reactive audio (laughter, emphasis).' },
        { key: "hook_strength", label: 'Hook strength', weight: 0.2, value: 0.79, contribution: 15.8, explainer: 'Does the first 2-3 seconds capture attention?' },
        { key: "storytelling", label: 'Storytelling / curiosity', weight: 0.2, value: 0.61, contribution: 12.2, explainer: 'Narrative shape: setup, turn and payoff.' },
        { key: "shareability", label: 'Shareability', weight: 0.15, value: 0.93, contribution: 13.95, explainer: 'Quotability, length fit and cleanliness of delivery.' },
        { key: "audio_visual_engagement", label: 'Audio & visual engagement', weight: 0.1, value: 0.95, contribution: 9.5, explainer: 'Loudness peaks, scene changes and visual highlights.' },
        { key: "standalone_context", label: 'Standalone context', weight: 0.15, value: 0.7, contribution: 10.5, explainer: 'Does the clip make sense without the full video?' },
      ],
      weights: { "emotional_intensity": 0.2, "hook_strength": 0.2, "storytelling": 0.2, "shareability": 0.15, "audio_visual_engagement": 0.1, "standalone_context": 0.15 },
    },
    rank: 3,
    status: "candidate",
    render_path: null,
    render_meta: {},
  },
];

const WEIGHTS: { label: string; pct: number; note: string }[] = [
  { label: "Emotional intensity", pct: 20, note: "How strongly the moment lands" },
  { label: "Hook strength", pct: 20, note: "What happens in the first three seconds" },
  { label: "Storytelling / curiosity", pct: 20, note: "Is there a reason to keep watching" },
  { label: "Shareability", pct: 15, note: "Would someone send this to a friend" },
  { label: "Standalone context", pct: 15, note: "Does it survive without the episode" },
  { label: "Audio and visual engagement", pct: 10, note: "Laughter, emphasis, motion" },
];

function Landing() {
  return (
    <>
      {/* 1. First viewport: the mechanism, at scale. ------------------- */}
      <section className="hero">
        <div className="shell">
          <h1 className="hero-title rise">
            Find the minute that matters in the three hours that don't.
          </h1>

          <div className="hero-grid">
            <div className="hero-copy rise" style={{ animationDelay: "80ms" }}>
              <p>
                Point QuickReel at a podcast, stream or lecture. It transcribes the
                whole thing, reads the conversation, and strikes out the few moments
                worth posting — then trims each one to its own sentence.
              </p>
              <div className="hero-actions">
                <Link to="/workbench" className="btn btn-primary">
                  Drop a video
                  <IconArrow size={18} />
                </Link>
                <a href="#how" className="btn btn-ghost">
                  See how a clip is cut
                </a>
              </div>
              <p className="hero-fine mono">
                9:16 · 1080×1920 · captions burned in
              </p>
            </div>

            <div className="hero-timeline rise" style={{ animationDelay: "160ms" }}>
              <Timeline durationSeconds={DEMO_DURATION} clips={DEMO_CLIPS} />
              <p className="demo-tag mono">
                Sample project — illustrative values, not a live result
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* 2. The cut, explained with one worked example. ---------------- */}
      <section className="section" id="how">
        <div className="shell">
          <h2 className="section-title">A moment is not a timestamp.</h2>
          <p className="section-lede">
            The interesting line starts at 10:32. The setup begins at 10:28. The
            laugh lands at 10:50. Cutting at 10:32 throws the clip away. QuickReel
            reads the transcript, the sentence boundaries and the audio as one
            piece of evidence and finds where the clip actually belongs.
          </p>

          <div className="cut-panel">
            <ol className="cut-steps">
              <li>
                <span className="mono cut-t">10:28</span>
                <span>
                  <strong>Setup begins.</strong> The speaker starts the thought that
                  makes the punchline make sense.
                </span>
              </li>
              <li className="is-detected">
                <span className="mono cut-t">10:32</span>
                <span>
                  <strong>The line.</strong> Detected as a moment; scoring starts
                  here, not the clip.
                </span>
              </li>
              <li>
                <span className="mono cut-t">10:48</span>
                <span>
                  <strong>Sentence ends.</strong> A clean boundary, so nothing is
                  cut mid-word.
                </span>
              </li>
              <li>
                <span className="mono cut-t">10:50</span>
                <span>
                  <strong>The room laughs.</strong> Payoff. Keep it — a clip that
                  ends on the reaction outperforms one that ends before it.
                </span>
              </li>
            </ol>

            <div className="cut-verdict">
              <span className="mono cut-verdict-cap">Delivered clip</span>
              <span className="cut-verdict-range mono">10:26 → 10:54</span>
              <span className="cut-verdict-note">
                Padding restored on both ends: two seconds of setup, four seconds of
                reaction, silence trimmed inside the span.
              </span>
            </div>
          </div>
        </div>
      </section>

      {/* 3. The pipeline, as a real sequence. ------------------------- */}
      <section className="section section-ink">
        <div className="shell">
          <h2 className="section-title">Six stages, each one doing one job.</h2>
          <div className="pipeline">
            {[
              ["Ingest", "Upload, an authorised URL, or a recording source."],
              ["Transcribe", "Whisper, word-level timestamps."],
              ["Understand", "A multimodal pass over frames and dialogue."],
              ["Detect", "Moment extraction, scoring and ranking."],
              ["Optimise", "Boundaries snapped to sentences and audio events."],
              ["Render", "FFmpeg cuts 9:16 with captions burnt in."],
            ].map(([name, detail], i) => (
              <div className="pipe-row" key={name}>
                <span className="mono pipe-i">{String(i + 1).padStart(2, "0")}</span>
                <span className="pipe-name">{name}</span>
                <span className="pipe-detail">{detail}</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* 4. The scoring model, stated honestly. ----------------------- */}
      <section className="section" id="scoring">
        <div className="shell score-layout">
          <div>
            <h2 className="section-title">What the score is made of.</h2>
            <p className="section-lede">
              Six weighted signals, each scored 0–1 and combined into one number out
              of 100. The weights are a starting assumption, not a law — they are
              meant to be recalibrated against real views, retention and shares.
            </p>

            <ul className="weights">
              {WEIGHTS.map((w) => (
                <li key={w.label}>
                  <span className="weight-pct mono">{w.pct}%</span>
                  <span className="weight-body">
                    <strong>{w.label}</strong>
                    <em>{w.note}</em>
                  </span>
                </li>
              ))}
            </ul>

            <p className="caveat">
              <IconWarn size={17} />
              Viral potential is a prediction, not a guarantee. Treat the ranking as
              a way to order review work, not as an outcome.
            </p>
          </div>

          <aside className="score-aside">
            <ScoreBreakdown clip={DEMO_CLIPS[0]} />
            <p className="demo-tag mono">
              Sample clip — “The biggest mistake I made”
            </p>
          </aside>
        </div>
      </section>

      {/* 5. What it will not do. Honesty as a feature. --------------- */}
      <section className="section" id="limits">
        <div className="shell">
          <h2 className="section-title">Where this stops.</h2>
          <div className="limits">
            <article className="limit">
              <h3 className="limit-h">YouTube downloads are blocked from some hosts</h3>
              <p>
                QuickReel can read a YouTube URL's metadata, but the media download is
                refused from datacenter IPs. The job fails loudly and names the reason
                — it never invents clips. Upload the file, or configure cookies or a
                proxy to ingest directly.
              </p>
            </article>
            <article className="limit">
              <h3 className="limit-h">Only ingest what you have the right to use</h3>
              <p>
                Point it at your own recordings, or material you are licensed to
                republish. QuickReel does not check rights for you.
              </p>
            </article>
            <article className="limit">
              <h3 className="limit-h">Face tracking arrives after the MVP</h3>
              <p>
                The current export is a centre crop — correct 9:16 framing, but it
                does not yet follow a speaker around the frame.
              </p>
            </article>
          </div>
        </div>
      </section>

      {/* 6. Close. ---------------------------------------------------- */}
      <section className="close">
        <div className="shell close-inner">
          <IconScissors size={30} />
          <h2 className="close-title">Bring a long video. Take out the short ones.</h2>
          <Link to="/workbench" className="btn btn-primary close-cta">
            Open the workbench
            <IconArrow size={18} />
          </Link>
        </div>
      </section>
    </>
  );
}

export default Landing;
