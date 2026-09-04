import type { WorkStatus } from "../lib/types";

export interface MessageCatalog {
  metadata: {
    title: string;
    description: string;
  };
  navigation: {
    environment: string;
    switchLanguage: string;
    switchLanguageAria: string;
  };
  dashboard: {
    eyebrow: string;
    heroLine: string;
    heroEmphasis: string;
    introduction: string;
    activeRuns: string;
    needAttention: string;
    totalWork: string;
    queue: string;
    developmentWork: string;
    noWorkTitle: string;
    noWorkBeforeLabel: string;
    noWorkAfterLabel: string;
  };
  create: {
    eyebrow: string;
    title: string;
    description: string;
    repository: string;
    repositoryPlaceholder: string;
    workTitle: string;
    titlePlaceholder: string;
    requirement: string;
    requirementPlaceholder: string;
    queuing: string;
    submit: string;
    error: string;
  };
  run: {
    back: string;
    liveStream: string;
    agentActivity: string;
    live: string;
    humanControl: string;
    title: string;
    openPullRequest: string;
    approve: string;
    approvalError: string;
    evidence: string;
    feedback: string;
    feedbackPlaceholder: string;
    feedbackSubmit: string;
    feedbackError: string;
    worker: string;
    unassigned: string;
    budget: string;
    minuteUnit: string;
    version: string;
  };
  status: Record<WorkStatus, string>;
  source: Record<"web" | "github" | "autonomous", string>;
}
