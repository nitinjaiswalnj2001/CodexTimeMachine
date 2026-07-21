import type {Event} from '../types/demo';
import {TimelineControls} from './TimelineControls';

const filters = ['messages', 'commands', 'files', 'evaluation', 'recommendation', 'completion'];
const category = (event: Event) => event.event_type.includes('MESSAGE') ? 'messages' : event.event_type.includes('COMMAND') ? (event.command_tags?.includes('evaluation') ? 'evaluation' : 'commands') : event.event_type.includes('FILE') ? 'files' : event.event_type.includes('COMPLETED') ? 'completion' : 'recommendation';

export function TemporalTimeline({baseline, replay, cursor, setCursor, playing, setPlaying, speed, setSpeed, enabled, setEnabled}: {baseline: Event[]; replay: Event[]; cursor: number; setCursor: (value: number) => void; playing: boolean; setPlaying: (value: boolean) => void; speed: number; setSpeed: (value: number) => void; enabled: Set<string>; setEnabled: (value: Set<string>) => void}) {
  const entries = [...baseline.map(event => ({...event, side: 'Baseline'})), ...replay.map(event => ({...event, side: 'Replay'}))].filter(event => enabled.has(category(event)));
  const selected = entries[cursor] ?? entries[0];
  if (!entries.length) return <div className="glass">No timeline events in accepted bundle.</div>;
  return <div>
    <h2>Temporal Timeline</h2>
    <div className="filter-row">{filters.map(filter => <label key={filter}><input type="checkbox" checked={enabled.has(filter)} onChange={() => { const next = new Set(enabled); if (next.has(filter)) next.delete(filter); else next.add(filter); setEnabled(next); }} />{filter}</label>)}</div>
    <TimelineControls playing={playing} onToggle={() => setPlaying(!playing)} onRestart={() => setCursor(0)} onPrevious={() => setCursor(Math.max(0, cursor - 1))} onNext={() => setCursor(Math.min(entries.length - 1, cursor + 1))} speed={speed} onSpeed={setSpeed} />
    <input aria-label="timeline scrubber" type="range" min="0" max={entries.length - 1} value={Math.min(cursor, entries.length - 1)} onChange={event => setCursor(+event.target.value)} />
    <div className="lane-label">Baseline History</div><div className="timeline">{entries.filter(event => event.side === 'Baseline').map(event => <EventCard event={event} selected={event.event_id === selected?.event_id} onSelect={() => setCursor(entries.indexOf(event))} key={`baseline-${event.event_id}`} />)}</div>
    <div className="lane-label">Future Evidence Boundary</div>
    <div className="lane-label">Counterfactual Replay</div><div className="timeline">{entries.filter(event => event.side === 'Replay').map(event => <EventCard event={event} selected={event.event_id === selected?.event_id} onSelect={() => setCursor(entries.indexOf(event))} key={`replay-${event.event_id}`} />)}</div>
    {selected && <div className="glass inspector"><b>{selected.side} event</b><code>{selected.event_id}</code><p>Sequence: {selected.sequence} · Status: {selected.status ?? 'Unavailable in accepted bundle'}</p><p>{selected.summary}</p>{selected.command && <code>{selected.command}</code>}{selected.output_preview && <pre>{selected.output_preview}</pre>}</div>}
  </div>;
}

function EventCard({event, selected, onSelect}: {event: Event & {side: string}; selected: boolean; onSelect: () => void}) {
  return <article className={selected ? 'selected' : ''} onClick={onSelect}><small>{event.side} / {event.sequence}</small><b>{event.event_type}</b><p>{event.summary}</p></article>;
}
