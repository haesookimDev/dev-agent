export type WorkStatus =
  | "queued"
  | "provisioning"
  | "analyzing"
  | "implementing"
  | "verifying"
  | "awaiting_feedback"
  | "awaiting_approval"
  | "awaiting_input"
  | "budget_exhausted"
  | "committing"
  | "pr_created"
  | "completed"
  | "failed"
  | "cancelled";

export interface WorkItem {
  id: string;
  correlation_id: string;
  source: "web" | "github" | "autonomous";
  source_external_id: string | null;
  title: string;
  requirement: string;
  repository: string;
  status: WorkStatus;
  version: number;
  requested_by: string;
  assigned_worker_id: string | null;
  budget_minutes: number;
  budget_cost: string | null;
  replan_limit: number;
  approval_required: boolean;
  github_installation_id: number | null;
  github_issue_number: number | null;
  pull_request_url: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentEvent {
  id: number;
  work_item_id: string;
  correlation_id: string;
  event_type: string;
  source: string;
  level: "debug" | "info" | "warning" | "error";
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Artifact {
  id: string;
  work_item_id: string;
  kind: string;
  name: string;
  content_type: string;
  size_bytes: number;
  created_at: string;
  expired_at?: string | null;
}
