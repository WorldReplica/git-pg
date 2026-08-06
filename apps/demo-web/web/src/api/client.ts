import type { components, paths } from "./schema";

export type TreeResponse = components["schemas"]["TreeResponse"];
export type FileVersionList = components["schemas"]["FileVersionList"];
export type FileVersionOut = components["schemas"]["FileVersionOut"];
export type FileCommentList = components["schemas"]["FileCommentList"];
export type FileCommentOut = components["schemas"]["FileCommentOut"];
export type AgentRunList = components["schemas"]["AgentRunList"];
export type AgentRunOut = components["schemas"]["AgentRunOut"];
export type AgentDiffResponse = components["schemas"]["AgentDiffResponse"];
export type ApproveResponse = components["schemas"]["ApproveResponse"];

export type AgentCommitOut = {
  oid: string;
  subject: string;
  author?: string | null;
};

export type AgentCommitsResponse = {
  run_id: string;
  base_commit: string;
  head_commit: string | null;
  commits: AgentCommitOut[];
};

type HttpMethod = "get" | "post" | "delete";

async function apiFetch<T>(path: string, init?: RequestInit & { method?: HttpMethod }): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || response.statusText);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export async function fetchTree(): Promise<TreeResponse> {
  return apiFetch<TreeResponse>("/api/tree?ref=main");
}

export async function fetchVersions(path: string): Promise<FileVersionList> {
  const encoded = path
    .split("/")
    .map((p) => encodeURIComponent(p))
    .join("/");
  return apiFetch<FileVersionList>(`/api/files/${encoded}/versions`);
}

export async function fetchComments(versionId: string): Promise<FileCommentList> {
  return apiFetch<FileCommentList>(`/api/file-versions/${versionId}/comments`);
}

export async function postComment(versionId: string, body: string): Promise<FileCommentOut> {
  return apiFetch<FileCommentOut>(`/api/file-versions/${versionId}/comments`, {
    method: "post",
    body: JSON.stringify({ body, author: "demo" }),
  });
}

export async function fetchAgents(): Promise<AgentRunList> {
  return apiFetch<AgentRunList>("/api/agents");
}

export async function spawnAgent(): Promise<{ run: AgentRunOut }> {
  type Spawn = paths["/api/agents"]["post"]["responses"]["200"]["content"]["application/json"];
  return apiFetch<Spawn>("/api/agents", {
    method: "post",
    body: JSON.stringify({}),
  });
}

export async function fetchAgentDiff(runId: string): Promise<AgentDiffResponse> {
  return apiFetch<AgentDiffResponse>(`/api/agents/${runId}/diff`);
}

export async function fetchAgentCommits(runId: string): Promise<AgentCommitsResponse> {
  return apiFetch<AgentCommitsResponse>(`/api/agents/${runId}/commits`);
}

export async function approveAgent(
  runId: string,
  rebaseStrategy: "auto" | "agent" = "auto",
): Promise<ApproveResponse> {
  return apiFetch<ApproveResponse>(`/api/agents/${runId}/approve`, {
    method: "post",
    body: JSON.stringify({ rebase_strategy: rebaseStrategy }),
  });
}

export async function rebaseAgent(
  runId: string,
  rebaseStrategy: "auto" | "agent" = "auto",
): Promise<{ run: AgentRunOut }> {
  return apiFetch<{ run: AgentRunOut }>(`/api/agents/${runId}/rebase`, {
    method: "post",
    body: JSON.stringify({ rebase_strategy: rebaseStrategy }),
  });
}

export async function rejectAgent(runId: string): Promise<{ run: AgentRunOut }> {
  return apiFetch<{ run: AgentRunOut }>(`/api/agents/${runId}/reject`, {
    method: "post",
  });
}

export async function deleteAgent(runId: string): Promise<{ run_id: string }> {
  return apiFetch<{ run_id: string }>(`/api/agents/${runId}`, {
    method: "delete",
  });
}

export function versionContentUrl(versionId: string): string {
  return `/api/file-versions/${versionId}/content`;
}
