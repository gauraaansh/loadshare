"use client";

import { formatDistanceToNow } from "date-fns";

interface Props {
  timestamp?: string | null;
  className?: string;
}

export function LastUpdated({ timestamp, className = "" }: Props) {
  if (!timestamp) return null;

  let label = "";
  try {
    label = formatDistanceToNow(new Date(timestamp), { addSuffix: true });
  } catch {
    return null;
  }

  return (
    <span className={`text-xs text-gray-500 ${className}`}>
      Updated {label}
    </span>
  );
}
