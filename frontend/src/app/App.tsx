import {useEffect, useMemo, useState} from 'react';
import {ChevronLeft, ChevronRight, Play, Wrench} from 'lucide-react';
import {loadBundle} from '../data/loadBundle';
import type {DemoBundle, Event} from '../types/demo';
import {AppShell} from '../components/AppShell';
import {MissionControl} from '../components/MissionControl';
import {TemporalTimeline} from '../components/TemporalTimeline';
import {EvidenceBoundary} from '../components/EvidenceBoundary';
import {GhostEngineerReveal} from '../components/GhostEngineerReveal';
import {HistoryComparison} from '../components/HistoryComparison';
import {TargetCoveragePanel} from '../components/TargetCoveragePanel';
import {ReasoningReceipt} from '../components/ReasoningReceipt';
import {JudgeMode} from '../components/JudgeMode';
import {TechnicalDrawer} from '../components/TechnicalDrawer';
import {LimitationBanner} from '../components/LimitationBanner';

type Mode = 'GUIDED_DEMO' | 'FREE_EXPLORE';
const phaseToScreen = [0, 0, 1, 2, 2, 3, 1, 4, 5];
const allFilters = new Set(['messages', 'commands', 'files', 'evaluation', 'recommendation', 'completion']);

function artifactEvents(artifact: unknown): Event[] {
  return ((artifact as {events?: Event[]})?.events ?? []) as Event[];
}

export default function App() {
  const [bundle, setBundle] = useState<DemoBundle>();
  const [error, setError] = useState('');
  const [mode, setMode] = useState<Mode>('FREE_EXPLORE');
  const [screen, setScreen] = useState(0);
  const [phase, setPhase] = useState(0);
  const [judgeStep, setJudgeStep] = useState(0);
  const [reduced, setReduced] = useState(() => localStorage.getItem('ctm-reduced') === 'true');
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [filters, setFilters] = useState(allFilters);

  useEffect(() => {
    loadBundle().then(setBundle).catch((reason: Error) => setError(reason.message));
  }, []);
  useEffect(() => localStorage.setItem('ctm-reduced', String(reduced)), [reduced]);

  const baseline = useMemo(() => artifactEvents(bundle?.artifacts.trajectory), [bundle]);
  const replay = useMemo(() => artifactEvents(bundle?.artifacts.replay_trajectory), [bundle]);
  const eventCount = baseline.length + replay.length;
  useEffect(() => {
    if (!playing || !eventCount) return;
    const timer = window.setInterval(() => setCursor(value => (value + 1) % eventCount), 1100 / speed);
    return () => window.clearInterval(timer);
  }, [eventCount, playing, speed]);

  if (error) return <main className="error"><h1>Evidence bundle unavailable</h1><p>{error}</p></main>;
  if (!bundle) return <main className="loading">Loading accepted evidence…</main>;
  const artifacts = bundle.artifacts;
  const clue = String(artifacts.intervention.clue ?? 'Unavailable in accepted bundle');
  const views = [
    <MissionControl key="mission" bundle={bundle} onGuided={() => { setJudgeStep(0); setMode('GUIDED_DEMO'); }} onExplore={() => setScreen(1)} />,
    <TemporalTimeline key="timeline" baseline={baseline} replay={replay} cursor={cursor} setCursor={setCursor} playing={playing} setPlaying={setPlaying} speed={speed} setSpeed={setSpeed} enabled={filters} setEnabled={setFilters} />,
    <EvidenceBoundary key="boundary" clue={clue} />,
    <GhostEngineerReveal key="ghost" artifact={artifacts.intervention} />,
    <HistoryComparison key="comparison" artifact={artifacts.divergence} />,
    <TargetCoveragePanel key="coverage" artifact={artifacts.counterfactual} />,
    <ReasoningReceipt key="receipt" bundle={bundle} />,
  ];

  if (mode === 'GUIDED_DEMO') {
    return <JudgeMode bundle={bundle} step={judgeStep} setStep={setJudgeStep} onExit={() => setMode('FREE_EXPLORE')} />;
  }

  return <AppShell phase={phase} onPhase={(selected) => { setPhase(selected); setScreen(phaseToScreen[selected]); }} reduced={reduced} onReduced={() => setReduced(value => !value)}>
    <header>
      <div>
        <p className="eyebrow">FREE EXPLORE / ACCEPTED EVIDENCE</p>
        <h1>Git remembers what changed.<br /><em>Codex Time Machine reconstructs why.</em></h1>
        <p className="lede">A forensic system for testing whether an AI engineering decision would change when given the minimum future evidence it originally lacked.</p>
      </div>
      <div className="integrity">LIVE MODEL INVOKED <b>FALSE</b><br />STATIC BUNDLE <b>ACCEPTED</b></div>
    </header>
    <nav className="screen-nav" aria-label="Demo modes"><button onClick={() => { setJudgeStep(0); setMode('GUIDED_DEMO'); }}><Play size={16} /> Guided Demo</button><button onClick={() => setScreen(0)}>Free Explore</button><button onClick={() => setDrawerOpen(true)}><Wrench size={16} /> Technical detail</button></nav>
    <LimitationBanner>This interface renders accepted observable evidence only; it does not reconstruct hidden reasoning or make a correctness claim.</LimitationBanner>
    {views[screen]}
    <footer><button aria-label="previous screen" onClick={() => setScreen(value => Math.max(0, value - 1))}><ChevronLeft /></button><span>{screen + 1} / {views.length}</span><button aria-label="next screen" onClick={() => setScreen(value => Math.min(views.length - 1, value + 1))}><ChevronRight /></button></footer>
    <TechnicalDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} bundle={bundle} />
  </AppShell>;
}
