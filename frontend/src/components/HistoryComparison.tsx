import {useRef} from 'react';
import type {Artifact} from '../types/demo';

type Highlight = {summary?: string; baseline_event_id?: string; replay_event_id?: string};

export function HistoryComparison({artifact}: {artifact: Artifact}) {
  const differences = artifact.event_differences ?? [];
  const rows = useRef<Record<string, HTMLElement | null>>({});
  const highlights: Array<[string, Highlight | undefined]> = [
    ['First structural divergence', artifact.first_structural_divergence as Highlight | undefined],
    ['First investigative divergence', artifact.first_investigative_divergence as Highlight | undefined],
    ['First replay evaluation divergence', artifact.first_replay_evaluation_divergence as Highlight | undefined],
  ];
  const select = (highlight: Highlight | undefined) => {
    const id = [...differences].find(d => d.baseline_event_ids.includes(highlight?.baseline_event_id ?? '') || d.replay_event_ids.includes(highlight?.replay_event_id ?? ''))?.difference_id;
    if (id) rows.current[id]?.scrollIntoView({behavior: 'smooth', block: 'center'});
  };
  return <div>
    <h2>Baseline History <span>vs</span> Counterfactual Replay</h2>
    <div className="divergence-highlights">{highlights.map(([label, highlight]) => <button key={label} onClick={() => select(highlight)} disabled={!highlight}><small>{label}</small><span>{highlight?.summary ?? 'Unavailable in accepted bundle'}</span></button>)}</div>
    <div className="comparison-grid">
      <div><h3>BASELINE HISTORY</h3>{differences.map(d => <article className={`diff ${d.difference_type}`} ref={node => { rows.current[d.difference_id] = node; }} key={`${d.difference_id}-b`}>{d.baseline_event_ids.length ? <><small>{d.difference_type}</small><p>{d.summary}</p></> : <p className="muted">No corresponding baseline event</p>}</article>)}</div>
      <div><h3>COUNTERFACTUAL REPLAY</h3>{differences.map(d => <article className={`diff ${d.difference_type}`} key={`${d.difference_id}-r`}>{d.replay_event_ids.length ? <><small>{d.difference_type}</small><p>{d.summary}</p></> : <p className="muted">No corresponding replay event</p>}</article>)}</div>
    </div>
  </div>;
}
