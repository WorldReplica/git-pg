export const queryKeys = {
  tree: ["tree", "main"] as const,
  versions: (path: string) => ["versions", path] as const,
  comments: (versionId: string) => ["comments", versionId] as const,
  agents: ["agents"] as const,
  agentDiff: (runId: string) => ["agent-diff", runId] as const,
  agentCommits: (runId: string) => ["agent-commits", runId] as const,
};
