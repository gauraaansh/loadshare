"use client";

import { AlertCircle } from "lucide-react";

interface Props {
  title?: string;
  message?: string;
  onRetry?: () => void;
}

export function PanelError({ title = "Panel error", message, onRetry }: Props) {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-3 py-10 text-center">
      <AlertCircle className="w-8 h-8 text-red-400 opacity-70" />
      <div>
        <p className="text-sm font-medium text-gray-300">{title}</p>
        {message && (
          <p className="text-xs text-gray-500 mt-1 max-w-xs">{message}</p>
        )}
      </div>
      {onRetry && (
        <button
          onClick={onRetry}
          className="text-xs text-ls-blue hover:underline mt-1"
        >
          Retry
        </button>
      )}
    </div>
  );
}
