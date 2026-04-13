"use client";

import { useState, useRef, useEffect } from "react";
import Link from "next/link";
import {
  ArrowLeft,
  Send,
  Bot,
  User,
  Loader2,
  ChevronDown,
  ChevronRight,
  GitBranch,
  Layers,
} from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Source {
  section:    string;
  similarity?: number;  // vector only
  excerpt:    string;
  expanded?:  boolean;
}

interface TraceBranch {
  chapter:  string;
  sections: string[];
}

interface Trace {
  tree?:    TraceBranch[];
  chapters: string[];
  sections: string[];
}

interface Message {
  role:      "user" | "assistant";
  content:   string;
  streaming?: boolean;
  sources?:  Source[];
  trace?:    Trace;
  mode?:     "vector" | "pageindex";
}

type Mode = "vector" | "pageindex";

// ── Constants ─────────────────────────────────────────────────────────────────

const SUGGESTED = [
  "What problem does ARIA solve, and what was the April 2023 crisis?",
  "How do the 5 LangGraph agents work together?",
  "What ML models are used and how were they trained?",
  "How does the Supervisor's RAG episodic memory work?",
  "Why was synthetic data used for 3 of 4 models?",
  "What is the full tech stack?",
];

// ── Source card ───────────────────────────────────────────────────────────────

function SourceCard({
  source,
  index,
  onToggle,
}: {
  source: Source;
  index:  number;
  onToggle: () => void;
}) {
  const simPct   = source.similarity != null ? Math.round(source.similarity * 100) : null;
  const simColor =
    simPct == null        ? "#6b7280"
    : simPct >= 80        ? "#22c55e"
    : simPct >= 65        ? "#eab308"
    :                       "#f97316";

  return (
    <div
      style={{
        background:   "#0F1117",
        border:       "1px solid rgba(255,255,255,0.07)",
        borderRadius: 10,
        marginBottom:  8,
        overflow:     "hidden",
      }}
    >
      {/* Header row */}
      <button
        onClick={onToggle}
        className="w-full text-left flex items-start gap-2 px-3 py-2.5"
        style={{ background: "transparent" }}
      >
        <span
          className="text-xs font-mono shrink-0 mt-0.5"
          style={{
            color:      "#4280FF",
            minWidth:   18,
            textAlign:  "right",
          }}
        >
          {index + 1}
        </span>
        <span className="flex-1 text-xs font-medium text-gray-300 leading-snug">
          {source.section}
        </span>
        <span className="flex items-center gap-1.5 shrink-0">
          {simPct != null && (
            <span
              className="text-xs font-semibold"
              style={{ color: simColor }}
            >
              {simPct}%
            </span>
          )}
          {source.expanded ? (
            <ChevronDown className="w-3.5 h-3.5 text-gray-600" />
          ) : (
            <ChevronRight className="w-3.5 h-3.5 text-gray-600" />
          )}
        </span>
      </button>

      {/* Excerpt */}
      {source.expanded && (
        <div
          className="px-3 pb-3 text-xs text-gray-500 leading-relaxed"
          style={{
            borderTop:  "1px solid rgba(255,255,255,0.05)",
            paddingTop: 10,
            whiteSpace: "pre-wrap",
          }}
        >
          {source.excerpt}
        </div>
      )}
    </div>
  );
}

// ── Trace graph ───────────────────────────────────────────────────────────────

function TraceGraph({ trace }: { trace: Trace }) {
  const tree = trace.tree ?? [];

  return (
    <div className="mb-5">
      {/* Header node */}
      <div className="flex flex-col items-center mb-1">
        <div
          className="flex items-center gap-2 px-3 py-2 rounded-xl text-xs font-semibold"
          style={{
            background: "#4280FF22",
            border:     "1px solid #4280FF55",
            color:      "#7ba7ff",
          }}
        >
          <GitBranch className="w-3.5 h-3.5" />
          Navigation Trace
        </div>
        {/* Trunk line */}
        <div style={{ width: 1, height: 16, background: "#4280FF44" }} />
      </div>

      {tree.map((branch, bi) => (
        <div key={bi} className="flex flex-col items-center">
          {/* Chapter node */}
          <div
            className="px-3 py-1.5 rounded-lg text-xs font-semibold max-w-full"
            style={{
              background: "#4280FF15",
              border:     "1px solid #4280FF44",
              color:      "#93bbff",
              textAlign:  "center",
            }}
          >
            {branch.chapter}
          </div>

          {/* Branch to sections */}
          {branch.sections.length > 0 && (
            <div className="w-full mt-1">
              {/* Vertical stem from chapter */}
              <div className="flex justify-center">
                <div style={{ width: 1, height: 12, background: "#4280FF33" }} />
              </div>

              {/* Horizontal bar spanning all sections */}
              {branch.sections.length > 1 && (
                <div className="relative flex justify-center">
                  <div
                    style={{
                      position:   "absolute",
                      top:        0,
                      left:       "10%",
                      right:      "10%",
                      height:     1,
                      background: "rgba(255,255,255,0.1)",
                    }}
                  />
                </div>
              )}

              {/* Section nodes */}
              <div
                className="flex flex-wrap justify-center gap-1.5 px-2"
                style={{ paddingTop: branch.sections.length > 1 ? 8 : 0 }}
              >
                {branch.sections.map((section, si) => (
                  <div key={si} className="flex flex-col items-center">
                    {branch.sections.length > 1 && (
                      <div style={{ width: 1, height: 8, background: "rgba(255,255,255,0.1)" }} />
                    )}
                    <div
                      className="px-2.5 py-1 rounded-md text-[11px] text-gray-400"
                      style={{
                        background: "rgba(255,255,255,0.04)",
                        border:     "1px solid rgba(255,255,255,0.08)",
                        maxWidth:   160,
                        textAlign:  "center",
                        lineHeight: "1.3",
                      }}
                    >
                      {section}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Spacer between chapters */}
          {bi < tree.length - 1 && (
            <div style={{ height: 12 }} />
          )}
        </div>
      ))}
    </div>
  );
}

// ── Sidebar ───────────────────────────────────────────────────────────────────

function SourcesSidebar({
  message,
  onToggleSource,
}: {
  message:       Message | null;
  onToggleSource: (msgIdx: number, srcIdx: number) => void;
  msgIdx:        number;
}) {
  if (!message) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center px-6">
        <Layers className="w-8 h-8 text-gray-700 mb-3" />
        <p className="text-gray-600 text-sm">
          Retrieved sources and retrieval trace will appear here after each response.
        </p>
      </div>
    );
  }

  const { sources = [], trace, mode, streaming } = message;

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Sidebar header */}
      <div
        className="px-4 py-3 shrink-0 flex items-center justify-between"
        style={{ borderBottom: "1px solid rgba(255,255,255,0.06)" }}
      >
        <span className="text-xs font-semibold text-gray-400 uppercase tracking-wide">
          Sources
        </span>
        <span
          className="text-xs px-2 py-0.5 rounded-full font-medium"
          style={{
            background: mode === "pageindex" ? "#a855f722" : "#4280FF22",
            color:      mode === "pageindex" ? "#a855f7"   : "#4280FF",
            border:     `1px solid ${mode === "pageindex" ? "#a855f733" : "#4280FF33"}`,
          }}
        >
          {mode === "pageindex" ? "PageIndex" : "Vector RAG"}
        </span>
      </div>

      {/* Scrollable content */}
      <div className="flex-1 overflow-y-auto px-4 py-4">
        {streaming && sources.length === 0 && (
          <div className="flex items-center gap-2 text-xs text-gray-600 mb-4">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            Retrieving…
          </div>
        )}

        {trace && (trace.tree?.length ?? 0) > 0 && (
          <TraceGraph trace={trace} />
        )}

        {sources.length > 0 && (
          <>
            <p className="text-xs text-gray-600 mb-3">
              {sources.length} section{sources.length !== 1 ? "s" : ""} retrieved
            </p>
            {sources.map((src, si) => (
              <SourceCard
                key={si}
                source={src}
                index={si}
                onToggle={() => onToggleSource(0 /* placeholder */, si)}
              />
            ))}
          </>
        )}
      </div>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function DocsChatClient() {
  const [messages, setMessages]           = useState<Message[]>([]);
  const [input, setInput]                 = useState("");
  const [loading, setLoading]             = useState(false);
  const [mode, setMode]                   = useState<Mode>("pageindex");
  const [activeMsgIdx, setActiveMsgIdx]   = useState<number>(-1);
  const bottomRef                         = useRef<HTMLDivElement>(null);
  const inputRef                          = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // When a new assistant message is added, auto-select it in the sidebar
  useEffect(() => {
    const lastIdx = messages.length - 1;
    if (messages[lastIdx]?.role === "assistant") {
      setActiveMsgIdx(lastIdx);
    }
  }, [messages.length]);

  function toggleSource(msgIdx: number, srcIdx: number) {
    setMessages((prev) => {
      const next = [...prev];
      const msg  = { ...next[msgIdx] };
      const srcs = [...(msg.sources ?? [])];
      srcs[srcIdx] = { ...srcs[srcIdx], expanded: !srcs[srcIdx].expanded };
      msg.sources  = srcs;
      next[msgIdx] = msg;
      return next;
    });
  }

  async function send(text: string) {
    const query = text.trim();
    if (!query || loading) return;

    setInput("");
    const userIdx = messages.length;
    setMessages((prev) => [...prev, { role: "user", content: query }]);
    setLoading(true);

    const assistantIdx = userIdx + 1;
    setMessages((prev) => [
      ...prev,
      { role: "assistant", content: "", streaming: true, mode },
    ]);
    setActiveMsgIdx(assistantIdx);

    try {
      const res = await fetch("/aria/api/docs-chat-v2", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ message: query, mode }),
      });

      if (!res.ok || !res.body) {
        const err = await res.json().catch(() => ({ error: "Unknown error" }));
        setMessages((prev) => {
          const next = [...prev];
          next[assistantIdx] = {
            role:    "assistant",
            content: err.error ?? "Something went wrong. Please try again.",
            mode,
          };
          return next;
        });
        return;
      }

      const reader  = res.body.getReader();
      const decoder = new TextDecoder();
      let   buffer  = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // Process complete SSE lines from buffer
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? ""; // keep incomplete last line

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const raw = line.slice(6).trim();
          if (!raw) continue;

          let evt: Record<string, unknown>;
          try {
            evt = JSON.parse(raw);
          } catch {
            continue;
          }

          const type = evt.type as string;

          if (type === "token") {
            const content = evt.content as string;
            setMessages((prev) => {
              const next = [...prev];
              const msg  = next[assistantIdx];
              next[assistantIdx] = {
                ...msg,
                content:   (msg.content ?? "") + content,
                streaming: true,
              };
              return next;
            });
          } else if (type === "sources") {
            const raw_sources = evt.sources as Array<{
              section:     string;
              similarity?: number;
              excerpt:     string;
            }>;
            setMessages((prev) => {
              const next = [...prev];
              next[assistantIdx] = {
                ...next[assistantIdx],
                sources: raw_sources.map((s) => ({ ...s, expanded: false })),
              };
              return next;
            });
          } else if (type === "trace") {
            setMessages((prev) => {
              const next = [...prev];
              next[assistantIdx] = {
                ...next[assistantIdx],
                trace: {
                  tree:     (evt.tree     as TraceBranch[]) ?? [],
                  chapters: (evt.chapters as string[])      ?? [],
                  sections: (evt.sections as string[])      ?? [],
                },
              };
              return next;
            });
          } else if (type === "error") {
            setMessages((prev) => {
              const next = [...prev];
              next[assistantIdx] = {
                role:    "assistant",
                content: (evt.detail as string) ?? "An error occurred.",
                mode,
              };
              return next;
            });
          } else if (type === "done") {
            setMessages((prev) => {
              const next = [...prev];
              next[assistantIdx] = { ...next[assistantIdx], streaming: false };
              return next;
            });
          }
        }
      }

      // Ensure streaming flag is cleared after stream ends
      setMessages((prev) => {
        const next = [...prev];
        if (next[assistantIdx]?.streaming) {
          next[assistantIdx] = { ...next[assistantIdx], streaming: false };
        }
        return next;
      });
    } catch {
      setMessages((prev) => {
        const next = [...prev];
        next[assistantIdx] = {
          role:    "assistant",
          content: "Network error. Please try again.",
          mode,
        };
        return next;
      });
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(input);
    }
  }

  const activeMsg =
    activeMsgIdx >= 0 && messages[activeMsgIdx]?.role === "assistant"
      ? messages[activeMsgIdx]
      : null;

  // Sidebar only appears after the first query is sent
  const showSidebar = messages.some((m) => m.role === "assistant");

  return (
    <div className="h-screen flex flex-col" style={{ background: "#0F1117" }}>
      {/* ── Header ── */}
      <header
        className="flex items-center justify-between px-6 py-3 shrink-0"
        style={{
          background:   "#1A1F2E",
          borderBottom: "1px solid rgba(255,255,255,0.06)",
        }}
      >
        <div className="flex items-center gap-4">
          <Link
            href="/"
            className="flex items-center gap-1.5 text-gray-500 hover:text-gray-300 transition-colors text-sm"
          >
            <ArrowLeft className="w-4 h-4" />
            Dashboard
          </Link>
          <div className="w-px h-5" style={{ background: "rgba(255,255,255,0.08)" }} />
          <div className="flex items-center gap-2">
            <span className="text-ls-blue font-bold text-lg tracking-tight">ARIA</span>
            <span className="text-gray-500 text-sm">Project Intelligence</span>
          </div>
        </div>

        {/* RAG mode toggle */}
        <div
          className="flex items-center rounded-lg p-0.5 text-xs"
          style={{ background: "#0F1117", border: "1px solid rgba(255,255,255,0.08)" }}
        >
          {(["vector", "pageindex"] as Mode[]).map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              disabled={loading}
              className="px-3 py-1.5 rounded-md transition-all font-medium disabled:opacity-50"
              style={{
                background: mode === m ? (m === "pageindex" ? "#a855f7" : "#4280FF") : "transparent",
                color:      mode === m ? "#fff" : "#6b7280",
              }}
            >
              {m === "vector" ? "Vector RAG" : "PageIndex"}
            </button>
          ))}
        </div>
      </header>

      {/* ── Split body ── */}
      <div className="flex flex-1 overflow-hidden">

        {/* ── Left: chat ── */}
        <div
          className="flex flex-col overflow-hidden"
          style={{
            width:      showSidebar ? "60%" : "100%",
            transition: "width 0.35s ease",
            borderRight: showSidebar ? "1px solid rgba(255,255,255,0.06)" : "none",
          }}
        >
          {/* Message list */}
          <div className="flex-1 overflow-y-auto">
            <div className="max-w-2xl mx-auto px-4 py-8">
              {messages.length === 0 ? (
                /* Empty state */
                <div className="flex flex-col items-center text-center mb-10">
                  <div
                    className="w-14 h-14 rounded-2xl flex items-center justify-center mb-5"
                    style={{ background: "#4280FF22", border: "1px solid #4280FF44" }}
                  >
                    <Bot className="w-7 h-7 text-ls-blue" />
                  </div>
                  <h1 className="text-xl font-semibold text-white mb-2">
                    Ask about the ARIA system
                  </h1>
                  <p className="text-gray-500 text-sm max-w-md">
                    I can answer technical questions about ARIA&apos;s architecture,
                    agents, ML models, data pipeline, and infrastructure decisions —
                    sourced directly from the project documentation.
                  </p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-8 w-full">
                    {SUGGESTED.map((q) => (
                      <button
                        key={q}
                        onClick={() => send(q)}
                        className="text-left px-4 py-3 rounded-xl text-sm text-gray-400 hover:text-white transition-colors"
                        style={{
                          background: "#1A1F2E",
                          border:     "1px solid rgba(255,255,255,0.06)",
                        }}
                        onMouseEnter={(e) =>
                          (e.currentTarget.style.borderColor = "#4280FF44")
                        }
                        onMouseLeave={(e) =>
                          (e.currentTarget.style.borderColor = "rgba(255,255,255,0.06)")
                        }
                      >
                        {q}
                      </button>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="space-y-6">
                  {messages.map((msg, i) => (
                    <div
                      key={i}
                      className={`flex gap-3 ${msg.role === "user" ? "flex-row-reverse" : "flex-row"}`}
                    >
                      {/* Avatar */}
                      <div
                        className="w-8 h-8 rounded-lg shrink-0 flex items-center justify-center"
                        style={{
                          background: msg.role === "user" ? "#4280FF22" : "#1A1F2E",
                          border:     msg.role === "user"
                            ? "1px solid #4280FF44"
                            : "1px solid rgba(255,255,255,0.06)",
                        }}
                      >
                        {msg.role === "user" ? (
                          <User className="w-4 h-4 text-ls-blue" />
                        ) : (
                          <Bot className="w-4 h-4 text-gray-400" />
                        )}
                      </div>

                      {/* Bubble */}
                      <div
                        className={`rounded-2xl px-4 py-3 text-sm leading-relaxed ${
                          msg.role === "user"
                            ? "text-white rounded-tr-sm"
                            : "text-gray-300 rounded-tl-sm"
                        }`}
                        style={{
                          background:  msg.role === "user" ? "#4280FF" : "#1A1F2E",
                          border:      msg.role === "user"
                            ? "none"
                            : "1px solid rgba(255,255,255,0.06)",
                          whiteSpace:  "pre-wrap",
                          maxWidth:    "85%",
                          cursor:      msg.role === "assistant" && msg.sources?.length
                            ? "pointer"
                            : "default",
                        }}
                        onClick={() => {
                          if (msg.role === "assistant") setActiveMsgIdx(i);
                        }}
                      >
                        {msg.content}
                        {msg.streaming && msg.content === "" && (
                          <span className="inline-flex items-center gap-1 text-gray-500">
                            <Loader2 className="w-3 h-3 animate-spin" />
                            Thinking…
                          </span>
                        )}
                        {msg.streaming && msg.content !== "" && (
                          <span className="inline-block w-0.5 h-4 bg-ls-blue animate-pulse ml-0.5 align-middle" />
                        )}
                        {/* Source count badge */}
                        {!msg.streaming && msg.role === "assistant" && msg.sources && msg.sources.length > 0 && (
                          <div className="mt-2 flex items-center gap-1.5">
                            <span
                              className="text-xs px-2 py-0.5 rounded-full"
                              style={{
                                background: i === activeMsgIdx
                                  ? (msg.mode === "pageindex" ? "#a855f722" : "#4280FF22")
                                  : "rgba(255,255,255,0.05)",
                                color: i === activeMsgIdx
                                  ? (msg.mode === "pageindex" ? "#a855f7" : "#4280FF")
                                  : "#6b7280",
                                border: `1px solid ${i === activeMsgIdx
                                  ? (msg.mode === "pageindex" ? "#a855f733" : "#4280FF33")
                                  : "rgba(255,255,255,0.06)"}`,
                              }}
                            >
                              {msg.sources.length} source{msg.sources.length !== 1 ? "s" : ""}
                              {i !== activeMsgIdx && " · click to view"}
                            </span>
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                  <div ref={bottomRef} />
                </div>
              )}
            </div>
          </div>

          {/* Input bar */}
          <div
            className="shrink-0 px-4 py-4"
            style={{ borderTop: "1px solid rgba(255,255,255,0.06)" }}
          >
            <div className="max-w-2xl mx-auto flex gap-3 items-end">
              <textarea
                ref={inputRef}
                rows={1}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Ask anything about the ARIA system…"
                disabled={loading}
                className="flex-1 resize-none rounded-xl px-4 py-3 text-sm text-white
                           outline-none placeholder-gray-600 disabled:opacity-50 transition-colors"
                style={{
                  background: "#1A1F2E",
                  border:     "1px solid rgba(255,255,255,0.1)",
                  maxHeight:  "120px",
                  lineHeight: "1.5",
                }}
                onFocus={(e) => (e.currentTarget.style.borderColor = "#4280FF88")}
                onBlur={(e)  => (e.currentTarget.style.borderColor = "rgba(255,255,255,0.1)")}
              />
              <button
                onClick={() => send(input)}
                disabled={loading || !input.trim()}
                className="w-10 h-10 rounded-xl flex items-center justify-center
                           transition-opacity disabled:opacity-30 disabled:cursor-not-allowed shrink-0"
                style={{ background: "#4280FF" }}
              >
                {loading ? (
                  <Loader2 className="w-4 h-4 text-white animate-spin" />
                ) : (
                  <Send className="w-4 h-4 text-white" />
                )}
              </button>
            </div>
            <p className="text-center text-gray-700 text-xs mt-2">
              Answers grounded in ARIA project documentation · May not reflect real-time state
            </p>
          </div>
        </div>

        {/* ── Right: sources sidebar ── */}
        <div
          className="flex flex-col overflow-hidden"
          style={{
            width:      showSidebar ? "40%" : "0%",
            opacity:    showSidebar ? 1 : 0,
            transition: "width 0.35s ease, opacity 0.35s ease",
            background: "#131720",
          }}
        >
          <SourcesSidebar
            message={activeMsg}
            onToggleSource={(_, srcIdx) => {
              if (activeMsgIdx >= 0) toggleSource(activeMsgIdx, srcIdx);
            }}
            msgIdx={activeMsgIdx}
          />
        </div>
      </div>
    </div>
  );
}
