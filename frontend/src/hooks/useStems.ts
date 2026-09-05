import { useQuery } from "@tanstack/react-query";
import { listStems } from "../api/client";

export function useStems(projectId: string) {
  return useQuery({
    queryKey: ["stems", projectId],
    queryFn: () => listStems(projectId),
  });
}
