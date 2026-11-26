export type VisualMatchResult = "match" | "mismatch" | "uncertain" | null;

export type LlmVerdict = "match" | "mismatch" | "uncertain" | null;

export type HumanDecision = "confirm" | "reject" | "pending" | null;

export type ReviewStatus = "pending" | "confirmed" | "rejected" | string;

export interface ReviewCandidate {
  id: number;
  city: string;
  site: string;
  representative_frame_path: string | null;
  group_name: string | null;
}

export interface ReviewItem {
  id: number;
  status: ReviewStatus;
  screenshot_paths: string[];
  candidate: ReviewCandidate | null;
  gps_distance_meters: number | null;
  video_filename: string;
  video_city: string | null;
  video_collected_at: string | null;
  video_location_confirmed: boolean;
  visual_match_result: VisualMatchResult;
  llm_verdict: LlmVerdict;
  human_decision: HumanDecision;
}
