import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { queryKeys } from "./queryKeys";

type WsMessage = {
  type: string;
  run_id?: string;
  version_ids?: string[];
  paths?: string[];
};

function wsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/api/ws`;
}

export function useLiveEvents(): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    const socket = new WebSocket(wsUrl());

    const onMessage = (event: MessageEvent<string>) => {
      let payload: WsMessage;
      try {
        payload = JSON.parse(event.data) as WsMessage;
      } catch {
        return;
      }
      switch (payload.type) {
        case "agent.updated":
          void queryClient.invalidateQueries({ queryKey: queryKeys.agents });
          if (payload.run_id) {
            void queryClient.invalidateQueries({
              queryKey: queryKeys.agentDiff(payload.run_id),
            });
            void queryClient.invalidateQueries({
              queryKey: queryKeys.agentCommits(payload.run_id),
            });
          }
          break;
        case "main.updated":
          void queryClient.invalidateQueries({ queryKey: queryKeys.tree });
          void queryClient.invalidateQueries({ queryKey: ["versions"] });
          break;
        case "file_versions.created":
          for (const path of payload.paths ?? []) {
            void queryClient.invalidateQueries({
              queryKey: queryKeys.versions(path),
            });
          }
          break;
        case "comment.created":
          void queryClient.invalidateQueries({ queryKey: ["comments"] });
          break;
        default:
          break;
      }
    };

    socket.addEventListener("message", onMessage);

    return () => {
      socket.removeEventListener("message", onMessage);
      socket.close();
    };
  }, [queryClient]);
}
