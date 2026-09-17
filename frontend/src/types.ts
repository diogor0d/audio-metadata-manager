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
  stats: Stats;
  capabilities: { ai: boolean; metube: boolean };
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
