import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import {
  approveAgent,
  deleteAgent,
  fetchAgentCommits,
  fetchAgentDiff,
  fetchAgents,
  fetchComments,
  fetchTree,
  fetchVersions,
  postComment,
  rebaseAgent,
  rejectAgent,
  spawnAgent,
  versionContentUrl,
  type AgentCommitOut,
  type AgentRunOut,
  type FileVersionOut,
} from "./api/client";
import { queryKeys } from "./api/queryKeys";
import { useLiveEvents } from "./api/useLiveEvents";
import "./app.css";

const REBASE_STRATEGY_KEY = "git-pg.rebaseStrategy";

function readRebaseStrategy(): "auto" | "agent" {
  try {
    const raw = localStorage.getItem(REBASE_STRATEGY_KEY);
    if (raw === "auto" || raw === "agent") {
      return raw;
    }
  } catch {
    // ignore private-mode / blocked storage
  }
  return "auto";
}

function writeRebaseStrategy(value: "auto" | "agent"): void {
  try {
    localStorage.setItem(REBASE_STRATEGY_KEY, value);
  } catch {
    // ignore
  }
}

export function App() {
  useLiveEvents();
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<FileVersionOut | null>(null);
  const [commentDraft, setCommentDraft] = useState("");
  const [expandedRun, setExpandedRun] = useState<string | null>(null);
  const [rebaseStrategy, setRebaseStrategy] = useState<"auto" | "agent">(readRebaseStrategy);
  const queryClient = useQueryClient();

  const treeQuery = useQuery({
    queryKey: queryKeys.tree,
    queryFn: fetchTree,
  });

  const versionsQuery = useQuery({
    queryKey: queryKeys.versions(selectedPath ?? ""),
    queryFn: () => fetchVersions(selectedPath!),
    enabled: selectedPath !== null,
  });

  const commentsQuery = useQuery({
    queryKey: queryKeys.comments(selectedVersion?.id ?? ""),
    queryFn: () => fetchComments(selectedVersion!.id),
    enabled: selectedVersion !== null,
  });

  const agentsQuery = useQuery({
    queryKey: queryKeys.agents,
    queryFn: fetchAgents,
  });

  const diffQuery = useQuery({
    queryKey: queryKeys.agentDiff(expandedRun ?? ""),
    queryFn: () => fetchAgentDiff(expandedRun!),
    enabled: expandedRun !== null,
  });

  const commitsQuery = useQuery({
    queryKey: queryKeys.agentCommits(expandedRun ?? ""),
    queryFn: () => fetchAgentCommits(expandedRun!),
    enabled: expandedRun !== null,
  });

  const spawnMutation = useMutation({
    mutationFn: spawnAgent,
    onSuccess: async (data) => {
      queryClient.setQueryData(queryKeys.agents, (prev: { runs: AgentRunOut[] } | undefined) => {
        const runs = prev?.runs ?? [];
        if (runs.some((r) => r.id === data.run.id)) {
          return prev;
        }
        return { runs: [data.run, ...runs] };
      });
      await queryClient.invalidateQueries({ queryKey: queryKeys.agents });
    },
  });

  const approveMutation = useMutation({
    mutationFn: ({ runId, strategy }: { runId: string; strategy: "auto" | "agent" }) =>
      approveAgent(runId, strategy),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.agents });
      await queryClient.invalidateQueries({ queryKey: queryKeys.tree });
    },
  });

  const rejectMutation = useMutation({
    mutationFn: rejectAgent,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.agents });
    },
  });

  const rebaseMutation = useMutation({
    mutationFn: ({ runId, strategy }: { runId: string; strategy: "auto" | "agent" }) =>
      rebaseAgent(runId, strategy),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.agents });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteAgent,
    onSuccess: async (_data, runId) => {
      setExpandedRun((cur) => (cur === runId ? null : cur));
      await queryClient.invalidateQueries({ queryKey: queryKeys.agents });
    },
  });

  const commentMutation = useMutation({
    mutationFn: ({ versionId, body }: { versionId: string; body: string }) =>
      postComment(versionId, body),
    onSuccess: async (_data, vars) => {
      setCommentDraft("");
      await queryClient.invalidateQueries({
        queryKey: queryKeys.comments(vars.versionId),
      });
    },
  });

  const previewText = useMemo(() => {
    if (!selectedVersion) {
      return null;
    }
    return versionContentUrl(selectedVersion.id);
  }, [selectedVersion]);

  return (
    <div className="layout">
      <header className="header">
        <h1>git-pg demo</h1>
        <p>File versions, comments, and agent approve on main</p>
      </header>

      <div className="panels">
        <section className="panel">
          <h2>Main tree</h2>
          {treeQuery.isLoading && <p>Loading…</p>}
          {treeQuery.error && <p className="error">{String(treeQuery.error)}</p>}
          <ul className="tree">
            {(treeQuery.data?.entries ?? []).map((entry) => (
              <li key={entry.path}>
                <button
                  type="button"
                  className={selectedPath === entry.path ? "tree-item active" : "tree-item"}
                  onClick={() => {
                    setSelectedPath(entry.path);
                    setSelectedVersion(null);
                  }}
                >
                  <span>{entry.path}</span>
                  <span className="muted">{entry.size} B</span>
                </button>
              </li>
            ))}
          </ul>
        </section>

        <section className="panel">
          <h2>Versions {selectedPath ? `· ${selectedPath}` : ""}</h2>
          {!selectedPath && <p className="muted">Select a file</p>}
          {selectedPath && versionsQuery.isLoading && <p>Loading…</p>}
          <ul className="versions">
            {(versionsQuery.data?.versions ?? []).map((version) => (
              <li key={version.id}>
                <button
                  type="button"
                  className={
                    selectedVersion?.id === version.id ? "version-item active" : "version-item"
                  }
                  onClick={() => setSelectedVersion(version)}
                >
                  <code>{version.id.slice(0, 8)}</code>
                  <span className="muted">{new Date(version.created_at).toLocaleString()}</span>
                </button>
              </li>
            ))}
          </ul>

          {selectedVersion && (
            <div className="detail">
              <h3>Preview</h3>
              {previewText && (
                <iframe title="version-preview" className="preview" src={previewText} />
              )}
              <h3>Comments</h3>
              <ul className="comments">
                {(commentsQuery.data?.comments ?? []).map((c) => (
                  <li key={c.id}>
                    <strong>{c.author}</strong>: {c.body}
                  </li>
                ))}
              </ul>
              <form
                className="comment-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (!commentDraft.trim() || !selectedVersion) {
                    return;
                  }
                  commentMutation.mutate({
                    versionId: selectedVersion.id,
                    body: commentDraft.trim(),
                  });
                }}
              >
                <input
                  value={commentDraft}
                  onChange={(e) => setCommentDraft(e.target.value)}
                  placeholder="Add a comment on this version…"
                />
                <button type="submit" disabled={commentMutation.isPending}>
                  Comment
                </button>
              </form>
            </div>
          )}
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>Agents</h2>
            <button
              type="button"
              onClick={() => spawnMutation.mutate()}
              disabled={spawnMutation.isPending}
            >
              Spawn agent
            </button>
          </div>
          <div className="rebase-field">
            <span className="rebase-field-label" id="rebase-strategy-label">
              Conflict resolution &amp; rebase strategy
            </span>
            <div className="rebase-switch" role="group" aria-labelledby="rebase-strategy-label">
              <button
                type="button"
                role="switch"
                aria-checked={rebaseStrategy === "agent"}
                aria-label="Conflict resolution and rebase strategy"
                className={rebaseStrategy === "agent" ? "switch-track on" : "switch-track"}
                onClick={() =>
                  setRebaseStrategy((cur) => {
                    const next = cur === "auto" ? "agent" : "auto";
                    writeRebaseStrategy(next);
                    return next;
                  })
                }
              >
                <span className="switch-option left">Auto rebase</span>
                <span className="switch-option right">Agent rebase</span>
                <span className="switch-thumb" />
              </button>
            </div>
          </div>
          {spawnMutation.error && <p className="error">{String(spawnMutation.error)}</p>}
          {approveMutation.error && <p className="error">{String(approveMutation.error)}</p>}
          {rebaseMutation.error && <p className="error">{String(rebaseMutation.error)}</p>}
          {deleteMutation.error && <p className="error">{String(deleteMutation.error)}</p>}
          <ul className="agents">
            {(agentsQuery.data?.runs ?? []).map((run) => (
              <AgentCard
                key={run.id}
                run={run}
                expanded={expandedRun === run.id}
                onToggle={() => setExpandedRun((cur) => (cur === run.id ? null : run.id))}
                diffPaths={expandedRun === run.id ? (diffQuery.data?.changed_paths ?? []) : []}
                commits={expandedRun === run.id ? (commitsQuery.data?.commits ?? []) : []}
                onApprove={() =>
                  approveMutation.mutate({ runId: run.id, strategy: rebaseStrategy })
                }
                onReject={() => rejectMutation.mutate(run.id)}
                onRebase={() => rebaseMutation.mutate({ runId: run.id, strategy: rebaseStrategy })}
                onDelete={() => deleteMutation.mutate(run.id)}
                busy={
                  approveMutation.isPending ||
                  rejectMutation.isPending ||
                  rebaseMutation.isPending ||
                  deleteMutation.isPending
                }
              />
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}

function AgentCard(props: {
  run: AgentRunOut;
  expanded: boolean;
  onToggle: () => void;
  diffPaths: { path: string; change: string }[];
  commits: AgentCommitOut[];
  onApprove: () => void;
  onReject: () => void;
  onRebase: () => void;
  onDelete: () => void;
  busy: boolean;
}) {
  const {
    run,
    expanded,
    onToggle,
    diffPaths,
    commits,
    onApprove,
    onReject,
    onRebase,
    onDelete,
    busy,
  } = props;
  const canDelete = run.status !== "approved";
  const baseStale = Boolean(run.base_stale);
  return (
    <li className="agent-card">
      <div className="agent-head-row">
        <button type="button" className="agent-head" onClick={onToggle}>
          <span>{run.branch}</span>
          <span className={`status status-${run.status}`}>
            {run.status}
            {baseStale ? " · stale base" : ""}
          </span>
        </button>
        <button
          type="button"
          className="agent-delete"
          aria-label={
            canDelete
              ? `Delete agent ${run.branch}`
              : `Approved agent ${run.branch} cannot be deleted`
          }
          title={canDelete ? "Delete agent session" : "Approved sessions cannot be deleted"}
          disabled={busy || !canDelete}
          onClick={(event) => {
            event.stopPropagation();
            if (canDelete) {
              onDelete();
            }
          }}
        >
          <TrashIcon />
        </button>
      </div>
      {expanded && (
        <div className="agent-body">
          {run.prompt ? (
            <p className="agent-prompt">{run.prompt}</p>
          ) : (
            run.status === "running" && <p className="muted">{run.summary ?? "Generating task…"}</p>
          )}
          {run.status === "running" && run.prompt && run.summary && (
            <p className="muted">{run.summary}</p>
          )}
          {run.status === "failed" && run.summary && (
            <pre className="agent-error">{run.summary.slice(-1200)}</pre>
          )}
          <p className="muted">base {run.base_commit.slice(0, 8)}</p>
          {run.head_commit && <p className="muted">head {run.head_commit.slice(0, 8)}</p>}
          {commits.length > 0 && (
            <div className="agent-commits">
              <p className="agent-commits-label">Commits ({commits.length})</p>
              <ol className="commit-list">
                {commits.map((c) => (
                  <li key={c.oid}>
                    <code className="commit-oid">{c.oid.slice(0, 8)}</code>
                    <span className="commit-subject">{c.subject}</span>
                  </li>
                ))}
              </ol>
            </div>
          )}
          {diffPaths.length === 0 ? (
            <p className="muted">
              {run.head_commit ? "No changed paths vs base." : "No head commit yet."}
            </p>
          ) : (
            <ul>
              {diffPaths.map((p) => (
                <li key={p.path}>
                  <code>{p.change}</code> {p.path}
                </li>
              ))}
            </ul>
          )}
          {run.status === "awaiting_approval" && (
            <div className="actions">
              {baseStale ? (
                <button type="button" disabled={busy} onClick={onRebase}>
                  Rebase onto main
                </button>
              ) : (
                <button type="button" disabled={busy} onClick={onApprove}>
                  Approve
                </button>
              )}
              <button type="button" disabled={busy} onClick={onReject}>
                Reject
              </button>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

function TrashIcon() {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <path d="M3 6h18" />
      <path d="M8 6V4h8v2" />
      <path d="M19 6l-1 14H6L5 6" />
      <path d="M10 11v6" />
      <path d="M14 11v6" />
    </svg>
  );
}
