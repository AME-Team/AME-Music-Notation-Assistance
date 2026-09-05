import { useQuery } from "@tanstack/react-query";
import { getPeaks } from "../api/client";

/** `name` は "original" またはステム名(#21)。 */
export function usePeaks(projectId: string, name: string) {
  return useQuery({
    queryKey: ["peaks", projectId, name],
    queryFn: () => getPeaks(projectId, name),
  });
}
