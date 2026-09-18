export type Tags = {
  title: string;
  artist: string;
  album: string;
  albumartist: string;
  date: string;
  genre: string;
  tracknumber: string;
};

export type Track = {
  id: string;
  relative_path: string;
  filename: string;
  status: "active" | "quarantined";
  size: number;
  mtime_ns: number;
  duration: number;
  bitrate: number;
  format: string;
  tags: Tags;
  issues: string[];
  has_artwork: boolean;
  suggested: { artist: string; title: string };
};

export type Stats = {
  total: number;
  active: number;
  quarantined: number;
  needs_attention: number;
  issues: Record<string, number>;
};

export type Bootstrap = {
  version: string;
  csrf_token: string;
  library_name: string;
  naming_template: string;
  stats: Stats;
  capabilities: { ai: boolean; metube: boolean };
};

export type AppSettings = {
  library_root: string;
  port: number;
  active_port: number;
  max_upload_mb: number;
  backup_retention_days: number;
  naming_template: string;
  ai: {
    base_url: string;
    model: string;
    api_key_set: boolean;
  };
  metube: {
    url: string;
    format: "mp3" | "m4a" | "opus";
    quality: "best" | "320" | "256" | "192" | "128";
    cf_client_id_set: boolean;
    cf_client_secret_set: boolean;
    api_key_set: boolean;
  };
};

export type HistoryItem = {
  id: string;
  kind: string;
  status: string;
  track_id: string | null;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  created_at: string;
  reversed_at: string | null;
};

export type AIPlan = {
  id: string;
  answer: string;
  naming_template: string | null;
  status: string;
  actions: Array<{
    type: "update_metadata";
    track_id: string;
    reason: string;
    tags: Partial<Tags>;
    filename: string | null;
  }>;
};

export type ArtworkCandidate = {
  release_group_id: string;
  release: string;
  artist: string;
  date: string;
  type: string;
  confidence: number;
  exact_track: boolean;
  exact_album: boolean;
};

export type DownloadJob = {
  id: string;
  status: string;
  error: string | null;
  purpose: "add" | "replace";
  target_track_id: string | null;
};
