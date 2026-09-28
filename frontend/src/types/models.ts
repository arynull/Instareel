export interface Account {
  id: number; username: string; ig_user_id: string | null; proxy_id: number | null; status: string;
  last_login: string | null; last_post: string | null; posts_today: number;
  max_daily_posts: number; cooldown_until: string | null; total_posts: number;
  total_views: number; total_likes: number; notes: string | null; has_session?: boolean;
  created_at: string; updated_at: string;
}

export interface Video {
  id: number; original_filename: string; duration: number | null; file_size: number | null;
  status: string; effect_preset: string | null; audio_track: string | null; is_trial: boolean;
  trial_strategy: string; custom_filters: string | null;
  trim_start: number | null; trim_end: number | null; failed_reason: string | null;
  source_caption: string | null; custom_thumbnail_path: string | null;
  processed_at: string | null; thumbnail_path: string | null; created_at: string;
}

export interface Post {
  id: number; video_id: number; account_id: number; ig_media_id: string | null;
  ig_permalink: string | null; caption: string; hashtags: string; status: string;
  scheduled_for: string | null; posted_at: string | null; audio_track: string | null; is_trial: boolean; views_24h: number | null;
  views_7d: number | null; likes_24h: number | null; engagement_rate: number | null;
  last_analytics_check: string | null;
  fail_reason: string | null; retry_count: number; created_at: string;
}

export interface ScheduleRule {
  id: number; name: string; day_of_week: number; hour: number; minute: number;
  account_id: number | null; is_active: boolean; preferred_effect: string | null;
  caption_template_id: number | null; prefer_source_caption: boolean; created_at?: string;
  pinned_video_id: number | null; pinned_video_label: string | null; pinned_video_status: string | null;
}

export interface Caption { id: number; name: string; content: string; category: string | null; is_active: boolean; use_count: number; avg_engagement: number | null; }
export interface HashtagSet { id: number; name: string; tags: string; is_active: boolean; use_count: number; }
export interface Bio { id: number; account_id: number; text: string; link_url: string; full_name: string; make_private: boolean | null; profile_pic_path: string | null; has_picture: boolean; last_applied: string | null; }
export interface IgProfile { username: string; full_name: string; biography: string; external_url: string; is_private: boolean; profile_pic_url: string; follower_count: number | null; following_count: number | null; media_count: number | null; }
export interface Proxy { id: number; url: string; protocol: string; username: string | null; country: string | null; is_healthy: boolean; last_checked: string | null; fail_count: number; latency_ms: number | null; last_error: string | null; source: string | null; is_active: boolean; created_at: string; }
export interface ProxyImportResult { added: number; duplicates_skipped: number; errors: { line: number; text: string; reason: string }[]; }
export interface ProxySource { id: number; name: string; url: string; default_protocol: string; default_country: string; is_active: boolean; last_fetch_at: string | null; last_added: number; last_total: number; }
export interface VideoSource {
  id: number; username: string; account_id: number | null;
  status: "idle" | "running" | "stopping" | "completed" | "failed";
  max_items: number; reels_only: boolean; with_covers: boolean; auto_process: boolean;
  delay_min_s: number; delay_max_s: number; has_cursor: boolean;
  fetched: number; downloaded: number; skipped: number; failed_count: number;
  total_items: number; pending_items: number;
  last_error: string | null; current_stage: string | null;
  started_at: string | null; finished_at: string | null; created_at: string;
}
export interface SourceItem {
  id: number; media_pk: string; shortcode: string; media_type: string;
  status: "pending" | "downloading" | "downloaded" | "skipped" | "failed";
  video_id: number | null; error: string | null; created_at: string;
}
export interface Effect { id: number; name: string; description: string; ffmpeg_filter: string; is_active: boolean; use_count: number; avg_engagement: number | null; }
export interface AudioTrack { id: number; name: string; description: string; file_path: string; duration: number | null; music_volume: number; duck_original: boolean; is_active: boolean; use_count: number; }
export interface AudioStats { id: number; name: string; posts: number; avg_engagement: number; views: number; use_count: number; is_active: boolean; }
export interface LogEntry { id: number; level: string; category: string; message: string; details: Record<string, unknown> | null; timestamp: string; }
export interface Setting { key: string; value: string; category: string; is_sensitive: boolean; }

export interface Overview {
  total_posts: number; total_views: number; avg_engagement_rate: number;
  active_accounts: number; queue_size: number; scheduled_count: number;
  series: { date: string; posts: number; views: number }[];
}
