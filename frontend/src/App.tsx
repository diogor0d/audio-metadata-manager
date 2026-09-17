import { FormEvent, useEffect, useRef, useState } from "react";
import {
  ArchiveRestore,
  Bot,
  Check,
  ChevronRight,
  Clock3,
  Disc3,
  FileAudio,
  FolderInput,
  History,
  Library,
  LoaderCircle,
  Music2,
  RefreshCw,
  Search,
  Settings2,
  Sparkles,
  Trash2,
  Upload,
  WandSparkles,
  X,
} from "lucide-react";
import { api, formatBytes, formatDuration, setRequestToken } from "./api";
import type { AIPlan, AppSettings, Bootstrap, HistoryItem, Stats, Tags, Track } from "./types";

type View = "library" | "add" | "assistant" | "history" | "quarantine" | "settings";

const tagFields: Array<[keyof Tags, string]> = [
  ["title", "Title"],
  ["artist", "Artist"],
  ["album", "Album"],
  ["albumartist", "Album artist"],
  ["date", "Date"],
  ["genre", "Genre"],
  ["tracknumber", "Track"],
];

const issueLabels: Record<string, string> = {
  missing_artist: "Missing artist",
  missing_title: "Missing title",
  missing_artwork: "No artwork",
};

async function fetchAllTracks(status: string, search = "", issue = "") {
  const items: Track[] = [];
  let stats: Stats | null = null;
  for (let offset = 0; ; offset += 500) {
    const params = new URLSearchParams({ status, limit: "500", offset: String(offset) });
    if (search) params.set("search", search);
    if (issue && status === "active") params.set("issue", issue);
    const page = await api<{ items: Track[]; stats: Stats }>(`/api/tracks?${params}`);
    items.push(...page.items);
    stats = page.stats;
    if (page.items.length < 500) break;
  }
  return { items, stats: stats! };
}

function App() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [tracks, setTracks] = useState<Track[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [selected, setSelected] = useState<Track | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [view, setView] = useState<View>("library");
  const [query, setQuery] = useState("");
  const [issue, setIssue] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const showError = (value: unknown) => {
    setError(value instanceof Error ? value.message : "The operation failed");
    setTimeout(() => setError(null), 5000);
  };

  const loadTracks = async (status = view === "quarantine" ? "quarantined" : "active") => {
    const data = await fetchAllTracks(status, query, issue);
    setTracks(data.items);
    setStats(data.stats);
    if (selected) {
      setSelected(data.items.find((item) => item.id === selected.id) || null);
    }
  };

  useEffect(() => {
    api<Bootstrap>("/api/bootstrap")
      .then((data) => {
        setBootstrap(data);
        setRequestToken(data.csrf_token);
        setStats(data.stats);
        return fetchAllTracks("active");
      })
      .then((data) => {
        setTracks(data.items);
        setStats(data.stats);
      })
      .catch(showError);
  }, []);

  useEffect(() => {
    if (!bootstrap || !["library", "quarantine"].includes(view)) return;
    const timer = setTimeout(() => loadTracks().catch(showError), 180);
    return () => clearTimeout(timer);
  }, [query, issue, view]);

  const selectView = (next: View) => {
    setView(next);
    setSelected(null);
    setIssue("");
  };

  const scan = async () => {
    setBusy(true);
    try {
      const result = await api<{ indexed: number; stats: Stats }>("/api/scan", { method: "POST" });
      setStats(result.stats);
      await loadTracks();
      setNotice(`Indexed ${result.indexed} audio files`);
    } catch (reason) {
      showError(reason);
    } finally {
      setBusy(false);
    }
  };

  const toggleChecked = (id: string) => {
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  if (!bootstrap || !stats) {
    return <div className="boot"><Disc3 className="spin" /><span>Reading the cue sheet…</span></div>;
  }

  return (
    <div className="app-shell">
      <aside className="side-rail">
        <button className="wordmark" onClick={() => selectView("library")}>
          <span className="wordmark-mark"><Disc3 /></span>
          <span>LINER</span>
        </button>
        <div className="library-stamp">
          <span>Source</span>
          <strong>{bootstrap.library_name}</strong>
          <small>Local · this computer</small>
        </div>
        <nav aria-label="Main navigation">
          <NavButton active={view === "library"} icon={<Library />} label="Library" count={stats.active} onClick={() => selectView("library")} />
          <NavButton active={view === "add"} icon={<FolderInput />} label="Add files" onClick={() => selectView("add")} />
          <NavButton active={view === "assistant"} icon={<Bot />} label="Assistant" onClick={() => selectView("assistant")} />
          <NavButton active={view === "history"} icon={<History />} label="History" onClick={() => selectView("history")} />
          <NavButton active={view === "quarantine"} icon={<Trash2 />} label="Quarantine" count={stats.quarantined} onClick={() => selectView("quarantine")} />
          <NavButton active={view === "settings"} icon={<Settings2 />} label="Settings" onClick={() => selectView("settings")} />
        </nav>
        <div className="rail-footer">
          <span className="status-dot" /> Loopback only
          <small>v{bootstrap.version}</small>
        </div>
      </aside>

      <main className="workbench">
        {view === "library" && (
          <LibraryView
            tracks={tracks}
            stats={stats}
            selected={selected}
            checked={checked}
            query={query}
            issue={issue}
            busy={busy}
            onQuery={setQuery}
            onIssue={setIssue}
            onScan={scan}
            onSelect={setSelected}
            onCheck={toggleChecked}
            onUpdated={async (track, message) => {
              setSelected(track);
              await loadTracks("active");
              setNotice(message);
            }}
            onError={showError}
            namingTemplate={bootstrap.naming_template}
          />
        )}
        {view === "add" && <AddView capabilities={bootstrap.capabilities} onAdded={async () => { await loadTracks("active"); setNotice("Added to the library inbox"); }} onError={showError} />}
        {view === "assistant" && <AssistantView enabled={bootstrap.capabilities.ai} tracks={tracks} checked={checked} selected={selected} onApplied={async () => { await loadTracks("active"); setNotice("AI plan applied"); }} onError={showError} />}
        {view === "history" && <HistoryView onChanged={() => loadTracks("active")} onError={showError} />}
        {view === "quarantine" && <QuarantineView tracks={tracks} onChanged={async (message) => { await loadTracks("quarantined"); setNotice(message); }} onError={showError} />}
        {view === "settings" && <SettingsView onSaved={(saved) => { setBootstrap({ ...bootstrap, library_name: saved.library_root.split(/[\\/]/).filter(Boolean).pop() || saved.library_root, naming_template: saved.naming_template, capabilities: { ai: Boolean(saved.ai.base_url && saved.ai.model && saved.ai.api_key_set), metube: Boolean(saved.metube.url) }, stats: saved.stats }); setStats(saved.stats); setNotice(saved.restart_required ? "Settings saved. Restart Liner to use the new port." : "Settings saved"); }} onError={showError} />}
      </main>

      <nav className="mobile-nav" aria-label="Mobile navigation">
        <MobileButton active={view === "library"} icon={<Library />} label="Library" onClick={() => selectView("library")} />
        <MobileButton active={view === "add"} icon={<Upload />} label="Add" onClick={() => selectView("add")} />
        <MobileButton active={view === "assistant"} icon={<Bot />} label="AI" onClick={() => selectView("assistant")} />
        <MobileButton active={view === "history"} icon={<History />} label="History" onClick={() => selectView("history")} />
        <MobileButton active={view === "quarantine"} icon={<Trash2 />} label="Bin" onClick={() => selectView("quarantine")} />
        <MobileButton active={view === "settings"} icon={<Settings2 />} label="Setup" onClick={() => selectView("settings")} />
      </nav>

      {(notice || error) && (
        <div className={`toast ${error ? "toast-error" : ""}`} role="status">
          {error || notice}<button aria-label="Dismiss" onClick={() => { setNotice(null); setError(null); }}><X /></button>
        </div>
      )}
    </div>
  );
}

function NavButton({ active, icon, label, count, onClick }: { active: boolean; icon: React.ReactNode; label: string; count?: number; onClick: () => void }) {
  return <button className={active ? "nav-item active" : "nav-item"} onClick={onClick}>{icon}<span>{label}</span>{count !== undefined && <b>{count}</b>}</button>;
}

function MobileButton({ active, icon, label, onClick }: { active: boolean; icon: React.ReactNode; label: string; onClick: () => void }) {
  return <button className={active ? "active" : ""} onClick={onClick}>{icon}<span>{label}</span></button>;
}

type LibraryProps = {
  tracks: Track[]; stats: Stats; selected: Track | null; checked: Set<string>; query: string; issue: string; busy: boolean;
  onQuery: (value: string) => void; onIssue: (value: string) => void; onScan: () => void; onSelect: (track: Track | null) => void;
  onCheck: (id: string) => void; onUpdated: (track: Track, message: string) => Promise<void>; onError: (error: unknown) => void;
  namingTemplate: string;
};

function LibraryView(props: LibraryProps) {
  return <div className="library-layout">
    <section className="queue-pane">
      <header className="page-head">
        <div><span className="eyebrow">Repair queue</span><h1>Your local cuts,<br /><em>made legible.</em></h1></div>
        <button className="icon-button" onClick={props.onScan} disabled={props.busy} title="Rescan library"><RefreshCw className={props.busy ? "spin" : ""} /></button>
      </header>
      <div className="health-strip">
        <div><strong>{props.stats.needs_attention}</strong><span>need attention</span></div>
        <div><strong>{props.stats.active - props.stats.needs_attention}</strong><span>ready</span></div>
        <div><strong>{props.checked.size}</strong><span>selected for AI</span></div>
      </div>
      <div className="filters">
        <label className="search"><Search /><input value={props.query} onChange={(event) => props.onQuery(event.target.value)} placeholder="Search title, artist or filename" /></label>
        <select value={props.issue} onChange={(event) => props.onIssue(event.target.value)} aria-label="Filter by issue">
          <option value="">All files</option>
          {Object.entries(issueLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </div>
      <div className="cue-header"><span /><span>Cut</span><span>Condition</span><span>Time</span></div>
      <div className="cue-list">
        {props.tracks.map((track) => <TrackRow key={track.id} track={track} active={props.selected?.id === track.id} checked={props.checked.has(track.id)} onCheck={() => props.onCheck(track.id)} onClick={() => props.onSelect(track)} />)}
        {!props.tracks.length && <div className="empty"><Music2 /><h2>No cuts on this sheet</h2><p>Change the filter or add an audio file.</p></div>}
      </div>
    </section>
    <section className={props.selected ? "editor-pane open" : "editor-pane"}>
      {props.selected ? <TrackEditor track={props.selected} namingTemplate={props.namingTemplate} onClose={() => props.onSelect(null)} onUpdated={props.onUpdated} onError={props.onError} /> : <div className="bench-empty"><div className="record-grooves"><Disc3 /></div><span>Track bench</span><p>Select a cut to inspect its label, hear it, and prepare corrections.</p></div>}
    </section>
  </div>;
}

function TrackRow({ track, active, checked, onCheck, onClick }: { track: Track; active: boolean; checked: boolean; onCheck: () => void; onClick: () => void }) {
  return <button className={active ? "cue-row active" : "cue-row"} onClick={onClick}>
    <span className="check-wrap" onClick={(event) => { event.stopPropagation(); onCheck(); }} role="checkbox" aria-checked={checked}>{checked ? <Check /> : <span />}</span>
    <span className="cut-title"><b>{track.tags.title || track.suggested.title || track.filename}</b><small>{track.tags.artist || track.suggested.artist || "Unknown artist"}</small></span>
    <span className={track.issues.length ? "condition needs" : "condition clean"}>{track.issues.length ? `${track.issues.length} ${track.issues.length === 1 ? "issue" : "issues"}` : "Checks pass"}</span>
    <span className="duration">{formatDuration(track.duration)}<ChevronRight /></span>
  </button>;
}

function TrackEditor({ track, namingTemplate, onClose, onUpdated, onError }: { track: Track; namingTemplate: string; onClose: () => void; onUpdated: (track: Track, message: string) => Promise<void>; onError: (error: unknown) => void }) {
  const [tags, setTags] = useState<Tags>(track.tags);
  const [filename, setFilename] = useState(track.filename);
  const [saving, setSaving] = useState(false);

  useEffect(() => { setTags(track.tags); setFilename(track.filename); }, [track.id, track.filename, track.tags]);
  const dirty = JSON.stringify(tags) !== JSON.stringify(track.tags) || filename !== track.filename;

  const save = async () => {
    setSaving(true);
    try {
      const updated = await api<Track>(`/api/tracks/${track.id}`, { method: "PATCH", body: JSON.stringify({ tags, filename }) });
      await onUpdated(updated, "Correction written and backed up");
    } catch (reason) { onError(reason); } finally { setSaving(false); }
  };
  const quarantine = async () => {
    if (!window.confirm(`Move “${track.filename}” to Liner's quarantine?`)) return;
    try { const updated = await api<Track>(`/api/tracks/${track.id}/quarantine`, { method: "POST" }); await onUpdated(updated, "Moved to quarantine"); onClose(); } catch (reason) { onError(reason); }
  };
  const uploadArtwork = async (file?: File) => {
    if (!file) return;
    const form = new FormData(); form.append("artwork", file);
    try { const updated = await api<Track>(`/api/tracks/${track.id}/artwork`, { method: "POST", body: form }); await onUpdated(updated, "Artwork embedded and backed up"); } catch (reason) { onError(reason); }
  };
  const applyNamingTemplate = async () => {
    try {
      const result = await api<{ filename: string }>(`/api/tracks/${track.id}/name-preview`, { method: "POST", body: JSON.stringify({ template: namingTemplate }) });
      setFilename(result.filename);
    } catch (reason) { onError(reason); }
  };

  return <div className="editor-card">
    <header className="editor-head"><span className="eyebrow">Track bench</span><button className="icon-button close-editor" onClick={onClose}><X /></button></header>
    <div className="track-identity">
      <label className={track.has_artwork ? "artwork" : "artwork missing"}>
        {track.has_artwork ? <img src={`/api/tracks/${track.id}/artwork`} alt="Embedded cover" /> : <><Disc3 /><span>Add cover</span></>}
        <input type="file" accept="image/jpeg,image/png" onChange={(event) => uploadArtwork(event.target.files?.[0])} />
      </label>
      <div><h2>{track.tags.title || track.suggested.title || "Untitled"}</h2><p>{track.tags.artist || track.suggested.artist || "Unknown artist"}</p><small>{track.format.toUpperCase()} · {formatBytes(track.size)} · {Math.round(track.bitrate / 1000)} kbps</small></div>
    </div>
    <audio controls preload="none" src={`/api/tracks/${track.id}/audio`} />
    {track.issues.length > 0 && <div className="issue-rack">{track.issues.map((item) => <span key={item}>{issueLabels[item] || item}</span>)}</div>}
    <div className="correction-label">
      <span>Current file</span><code>{track.filename}</code>
      <i>correction strip</i>
      <input value={filename} onChange={(event) => setFilename(event.target.value)} aria-label="Proposed filename" />
    </div>
    <button className="suggestion" onClick={applyNamingTemplate}><WandSparkles /> Apply naming pattern: {namingTemplate}</button>
    {!track.tags.artist && track.suggested.artist && <button className="suggestion" onClick={() => setTags({ ...tags, ...track.suggested })}><WandSparkles /> Use filename reading: {track.suggested.artist} — {track.suggested.title}</button>}
    <div className="tag-form">{tagFields.map(([field, label]) => <label key={field}><span>{label}</span><input value={tags[field]} onChange={(event) => setTags({ ...tags, [field]: event.target.value })} /></label>)}</div>
    <div className="editor-actions"><button className="danger-quiet" onClick={quarantine}><Trash2 /> Quarantine</button><button className="primary" disabled={!dirty || saving} onClick={save}>{saving ? <LoaderCircle className="spin" /> : <Check />} Save correction</button></div>
  </div>;
}

function AddView({ capabilities, onAdded, onError }: { capabilities: Bootstrap["capabilities"]; onAdded: () => Promise<void>; onError: (error: unknown) => void }) {
  const [file, setFile] = useState<File | null>(null); const [url, setUrl] = useState(""); const [busy, setBusy] = useState(false); const [job, setJob] = useState<{ id: string; status: string } | null>(null);
  const upload = async (event: FormEvent) => { event.preventDefault(); if (!file) return; setBusy(true); const form = new FormData(); form.append("audio_file", file); try { await api("/api/uploads", { method: "POST", body: form }); setFile(null); await onAdded(); } catch (reason) { onError(reason); } finally { setBusy(false); } };
  const download = async (event: FormEvent) => { event.preventDefault(); setBusy(true); try { const created = await api<{ id: string; status: string }>("/api/metube/jobs", { method: "POST", body: JSON.stringify({ url }) }); setJob(created); } catch (reason) { onError(reason); } finally { setBusy(false); } };
  const checkJob = async () => { if (!job) return; try { const current = await api<{ id: string; status: string }>(`/api/metube/jobs/${job.id}`); setJob(current); } catch (reason) { onError(reason); } };
  const importJob = async () => { if (!job) return; setBusy(true); try { await api(`/api/metube/jobs/${job.id}/import`, { method: "POST" }); setJob(null); setUrl(""); await onAdded(); } catch (reason) { onError(reason); } finally { setBusy(false); } };
  return <Page title="Bring in a new cut" eyebrow="Ingest desk" intro="Files are validated in staging before Spotify can see them.">
    <div className="intake-grid">
      <form className="intake-card" onSubmit={upload}><span className="card-index">FILE</span><Upload /><h2>From this computer</h2><p>Add MP3, M4A, MP4, FLAC, Ogg, Opus or WAV. Name collisions are preserved as separate copies.</p><label className="file-pick"><input type="file" accept="audio/*,.m4a,.flac,.opus" onChange={(event) => setFile(event.target.files?.[0] || null)} /><FileAudio />{file ? file.name : "Choose audio file"}</label><button className="primary" disabled={!file || busy}>{busy ? <LoaderCircle className="spin" /> : <FolderInput />} Validate and add</button></form>
      <form className={capabilities.metube ? "intake-card" : "intake-card disabled"} onSubmit={download}><span className="card-index">URL</span><Disc3 /><h2>Through MeTube</h2><p>Request audio from your configured sidecar, then import the completed result into the same staging flow.</p>{capabilities.metube ? <><input type="url" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://…" required /><button className="secondary" disabled={busy}>Request audio</button>{job && <div className="job-strip"><span>{job.status}</span>{job.status === "complete" ? <button type="button" onClick={importJob}>Import file</button> : <button type="button" onClick={checkJob}>Check progress</button>}</div>}</> : <div className="not-configured">Configure the MeTube connection in Settings to enable this source.</div>}</form>
    </div>
  </Page>;
}

function AssistantView({ enabled, tracks, checked, selected, onApplied, onError }: { enabled: boolean; tracks: Track[]; checked: Set<string>; selected: Track | null; onApplied: () => Promise<void>; onError: (error: unknown) => void }) {
  const [prompt, setPrompt] = useState(""); const [plan, setPlan] = useState<AIPlan | null>(null); const [busy, setBusy] = useState(false);
  const ids = checked.size ? Array.from(checked) : selected ? [selected.id] : [];
  const create = async (event: FormEvent) => { event.preventDefault(); setBusy(true); try { setPlan(await api<AIPlan>("/api/ai/plans", { method: "POST", body: JSON.stringify({ prompt, track_ids: ids }) })); } catch (reason) { onError(reason); } finally { setBusy(false); } };
  const apply = async () => { if (!plan) return; setBusy(true); try { await api(`/api/ai/plans/${plan.id}/apply`, { method: "POST" }); setPlan(null); await onApplied(); } catch (reason) { onError(reason); } finally { setBusy(false); } };
  return <Page title="Ask for a plan, not a guess" eyebrow="Metadata assistant" intro="The model can propose corrections. It cannot touch a file until you approve this sheet.">
    {!enabled ? <div className="assistant-off"><Bot /><h2>Assistant not configured</h2><p>Add an OpenAI-compatible endpoint, model and API key in Settings. Core library management remains available without AI.</p></div> : <div className="assistant-layout"><form className="prompt-card" onSubmit={create}><label>Selected material <strong>{ids.length} {ids.length === 1 ? "track" : "tracks"}</strong></label><div className="selected-chips">{ids.slice(0, 8).map((id) => <span key={id}>{tracks.find((track) => track.id === id)?.filename || id}</span>)}</div><textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Standardize artist and title, preserve remix labels, and propose Artist - Title (Version) filenames." /><button className="primary" disabled={!prompt || !ids.length || busy}><Sparkles /> Draft correction plan</button></form>{plan && <section className="plan-sheet"><span className="eyebrow">Proposed plan</span><p>{plan.answer}</p>{plan.naming_template && <code>{plan.naming_template}</code>}<div className="plan-actions">{plan.actions.map((action, index) => <article key={`${action.track_id}-${index}`}><b>{tracks.find((track) => track.id === action.track_id)?.filename}</b><p>{action.reason}</p><small>{Object.entries(action.tags).map(([key, value]) => `${key}: ${value}`).join(" · ")}</small>{action.filename && <code>{action.filename}</code>}</article>)}</div><button className="primary" onClick={apply} disabled={!plan.actions.length || busy}><Check /> Apply {plan.actions.length} corrections</button></section>}</div>}
  </Page>;
}

function HistoryView({ onChanged, onError }: { onChanged: () => Promise<void>; onError: (error: unknown) => void }) {
  const [items, setItems] = useState<HistoryItem[]>([]); const load = () => api<{ items: HistoryItem[] }>("/api/history").then((data) => setItems(data.items)).catch(onError); useEffect(() => { void load(); }, []);
  const undo = async (id: string) => { try { await api(`/api/history/${id}/undo`, { method: "POST" }); await onChanged(); load(); } catch (reason) { onError(reason); } };
  return <Page title="Every cut leaves a trail" eyebrow="Change log" intro="Tag edits keep an original backup. Quarantines can be restored from here or from the bin."><div className="history-list">{items.map((item) => <article key={item.id}><span className={`operation-mark ${item.kind}`}><Clock3 /></span><div><b>{item.kind}</b><p>{describeOperation(item)}</p><small>{new Date(item.created_at).toLocaleString()}</small></div>{item.status === "applied" && ["edit", "artwork", "quarantine"].includes(item.kind) && <button className="secondary compact" onClick={() => undo(item.id)}>Undo</button>}</article>)}{!items.length && <div className="empty"><History /><h2>No changes yet</h2></div>}</div></Page>;
}

function describeOperation(item: HistoryItem) { const before = item.before.relative_path as string | undefined; const after = item.after.relative_path as string | undefined; if (item.kind === "edit" && before !== after) return `${before} → ${after}`; if (item.kind === "quarantine") return `Removed ${before} from the active library`; if (item.kind === "add") return `Added ${after}`; return before || after || "Library operation"; }

function QuarantineView({ tracks, onChanged, onError }: { tracks: Track[]; onChanged: (message: string) => Promise<void>; onError: (error: unknown) => void }) {
  const restore = async (track: Track) => { try { await api(`/api/tracks/${track.id}/restore`, { method: "POST" }); await onChanged("Restored to the library"); } catch (reason) { onError(reason); } };
  const purge = async (track: Track) => { if (!window.confirm(`Permanently delete “${track.filename}”? This cannot be undone.`)) return; try { await api(`/api/tracks/${track.id}`, { method: "DELETE" }); await onChanged("Permanently deleted"); } catch (reason) { onError(reason); } };
  return <Page title="Nothing disappears by accident" eyebrow="Quarantine" intro="Files stay outside Spotify's source until you restore or permanently delete them."><div className="quarantine-list">{tracks.map((track) => <article key={track.id}><div className="mini-art"><Disc3 /></div><div><b>{track.tags.title || track.filename}</b><p>{track.tags.artist || "Unknown artist"}</p><code>{track.filename}</code></div><div className="row-actions"><button className="secondary compact" onClick={() => restore(track)}><ArchiveRestore /> Restore</button><button className="danger-quiet" onClick={() => purge(track)}><Trash2 /> Delete forever</button></div></article>)}{!tracks.length && <div className="empty"><Check /><h2>Quarantine is empty</h2><p>No files are waiting for a decision.</p></div>}</div></Page>;
}

type SavedSettings = AppSettings & { restart_required: boolean; stats: Stats };

function SettingsView({ onSaved, onError }: { onSaved: (settings: SavedSettings) => void; onError: (error: unknown) => void }) {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const originalLibrary = useRef("");
  const [secrets, setSecrets] = useState({ ai: "", cfId: "", cfSecret: "", metube: "" });
  const [clear, setClear] = useState({ ai: false, cfId: false, cfSecret: false, metube: false });
  const [saving, setSaving] = useState(false);
  useEffect(() => { api<AppSettings>("/api/settings").then((value) => { originalLibrary.current = value.library_root; setSettings(value); }).catch(onError); }, []);
  if (!settings) return <div className="boot"><Disc3 className="spin" /><span>Opening settings…</span></div>;
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (settings.library_root !== originalLibrary.current && !window.confirm("Change the library folder? This rebuilds the index and clears its history, without modifying either library's audio files.")) return;
    setSaving(true);
    try {
      const saved = await api<SavedSettings>("/api/settings", { method: "PUT", body: JSON.stringify({
        library_root: settings.library_root, port: settings.port, max_upload_mb: settings.max_upload_mb, backup_retention_days: settings.backup_retention_days, naming_template: settings.naming_template,
        ai: { base_url: settings.ai.base_url, model: settings.ai.model, api_key: secrets.ai || null, clear_api_key: clear.ai },
        metube: { url: settings.metube.url, format: settings.metube.format, quality: settings.metube.quality, cf_client_id: secrets.cfId || null, cf_client_secret: secrets.cfSecret || null, api_key: secrets.metube || null, clear_cf_client_id: clear.cfId, clear_cf_client_secret: clear.cfSecret, clear_api_key: clear.metube },
      }) });
      originalLibrary.current = saved.library_root; setSettings(saved); setSecrets({ ai: "", cfId: "", cfSecret: "", metube: "" }); setClear({ ai: false, cfId: false, cfSecret: false, metube: false }); onSaved(saved);
    } catch (reason) { onError(reason); } finally { setSaving(false); }
  };
  const secretField = (label: string, value: string, setter: (value: string) => void, configured: boolean, clearValue: boolean, clearSetter: (value: boolean) => void) => <label><span>{label}</span><input type="password" value={value} onChange={(event) => { setter(event.target.value); clearSetter(false); }} placeholder={configured ? "Saved securely · leave blank to keep" : "Not configured"} autoComplete="off" /><small><input type="checkbox" checked={clearValue} onChange={(event) => { clearSetter(event.target.checked); setter(""); }} /> Clear saved value</small></label>;
  return <Page title="Set the working rules" eyebrow="Local configuration" intro="Settings stay on this Windows account. Credentials are protected with Windows DPAPI and never returned to this page.">
    <form className="settings-form" onSubmit={save}>
      <section className="settings-card"><span className="card-index">LIBRARY</span><h2>Source and naming</h2><label><span>Audio folder</span><input value={settings.library_root} onChange={(event) => setSettings({ ...settings, library_root: event.target.value })} /></label><label><span>Filename pattern</span><input value={settings.naming_template} onChange={(event) => setSettings({ ...settings, naming_template: event.target.value })} /><small>Fields: {"{artist} {title} {album} {albumartist} {date} {year} {genre} {track}"}</small></label></section>
      <section className="settings-card"><span className="card-index">RUNTIME</span><h2>Limits and retention</h2><div className="settings-pair"><label><span>Upload limit (MB)</span><input type="number" min="1" max="4096" value={settings.max_upload_mb} onChange={(event) => setSettings({ ...settings, max_upload_mb: Number(event.target.value) })} /></label><label><span>Backup retention (days)</span><input type="number" min="1" max="3650" value={settings.backup_retention_days} onChange={(event) => setSettings({ ...settings, backup_retention_days: Number(event.target.value) })} /></label></div><label><span>Loopback port</span><input type="number" min="1024" max="65535" value={settings.port} onChange={(event) => setSettings({ ...settings, port: Number(event.target.value) })} /><small>Currently using {settings.active_port}. A changed port takes effect after restarting Liner.</small></label></section>
      <section className="settings-card"><span className="card-index">AI</span><h2>Metadata assistant</h2><label><span>OpenAI-compatible endpoint</span><input type="url" value={settings.ai.base_url} onChange={(event) => setSettings({ ...settings, ai: { ...settings.ai, base_url: event.target.value } })} placeholder="https://api.example.com/v1" /></label><label><span>Model</span><input value={settings.ai.model} onChange={(event) => setSettings({ ...settings, ai: { ...settings.ai, model: event.target.value } })} /></label>{secretField("API key", secrets.ai, (value) => setSecrets({ ...secrets, ai: value }), settings.ai.api_key_set, clear.ai, (value) => setClear({ ...clear, ai: value }))}</section>
      <section className="settings-card"><span className="card-index">METUBE</span><h2>Download sidecar</h2><label><span>Sidecar endpoint</span><input type="url" value={settings.metube.url} onChange={(event) => setSettings({ ...settings, metube: { ...settings.metube, url: event.target.value } })} placeholder="https://metube.example.com" /></label><div className="settings-pair"><label><span>Format</span><select value={settings.metube.format} onChange={(event) => setSettings({ ...settings, metube: { ...settings.metube, format: event.target.value as AppSettings["metube"]["format"] } })}><option>mp3</option><option>m4a</option><option>opus</option></select></label><label><span>Quality</span><select value={settings.metube.quality} onChange={(event) => setSettings({ ...settings, metube: { ...settings.metube, quality: event.target.value as AppSettings["metube"]["quality"] } })}><option>best</option><option>320</option><option>256</option><option>192</option><option>128</option></select></label></div>{secretField("Cloudflare client ID", secrets.cfId, (value) => setSecrets({ ...secrets, cfId: value }), settings.metube.cf_client_id_set, clear.cfId, (value) => setClear({ ...clear, cfId: value }))}{secretField("Cloudflare client secret", secrets.cfSecret, (value) => setSecrets({ ...secrets, cfSecret: value }), settings.metube.cf_client_secret_set, clear.cfSecret, (value) => setClear({ ...clear, cfSecret: value }))}{secretField("Sidecar API key", secrets.metube, (value) => setSecrets({ ...secrets, metube: value }), settings.metube.api_key_set, clear.metube, (value) => setClear({ ...clear, metube: value }))}</section>
      <div className="settings-actions"><p>Changing the source rebuilds the index and clears history. Quarantined files must be resolved first.</p><button className="primary" disabled={saving}>{saving ? <LoaderCircle className="spin" /> : <Check />} Save settings</button></div>
    </form>
  </Page>;
}

function Page({ title, eyebrow, intro, children }: { title: string; eyebrow: string; intro: string; children: React.ReactNode }) { return <section className="page"><header className="page-head solo"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p>{intro}</p></div></header>{children}</section>; }

export default App;
