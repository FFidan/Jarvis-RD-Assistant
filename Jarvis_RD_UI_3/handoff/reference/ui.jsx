// Shared UI primitives + Lucide icons (inline SVG) for the My Day variants

const cn = (...parts) => parts.filter(Boolean).join(" ");

// Minimal Lucide-style icons drawn inline so we don't depend on a CDN
function Icon({ name, className = "h-4 w-4" }) {
  const stroke = { fill: "none", stroke: "currentColor", strokeWidth: 1.75, strokeLinecap: "round", strokeLinejoin: "round" };
  const paths = {
    Sun: <><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></>,
    Sparkles: <><path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/></>,
    Inbox: <><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11Z"/></>,
    Brain: <><path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/></>,
    Folder: <><path d="M20 19a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2Z"/></>,
    Play: <polygon points="6 3 20 12 6 21 6 3"/>,
    Pause: <><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></>,
    Stop: <rect x="5" y="5" width="14" height="14" rx="1"/>,
    Plus: <><path d="M12 5v14M5 12h14"/></>,
    Check: <polyline points="20 6 9 17 4 12"/>,
    ChevronRight: <polyline points="9 18 15 12 9 6"/>,
    ChevronDown: <polyline points="6 9 12 15 18 9"/>,
    ChevronLeft: <polyline points="15 18 9 12 15 6"/>,
    ChevronsRight: <><polyline points="13 17 18 12 13 7"/><polyline points="6 17 11 12 6 7"/></>,
    ArrowRight: <><path d="M5 12h14M13 5l7 7-7 7"/></>,
    Search: <><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></>,
    Bookmark: <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>,
    BookmarkFilled: <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" fill="currentColor"/>,
    ThumbsUp: <path d="M7 10v12M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/>,
    ThumbsDown: <path d="M17 14V2M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/>,
    Trash: <><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6m5 0V4a2 2 0 0 1 2-2h0a2 2 0 0 1 2 2v2"/></>,
    X: <><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></>,
    Star: <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>,
    StarFilled: <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" fill="currentColor"/>,
    Activity: <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>,
    Calendar: <><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></>,
    Flame: <path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>,
    Clock: <><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></>,
    Quote: <><path d="M3 21c0-2.5 1-7 8-7"/><path d="M3 14V8a2 2 0 0 1 2-2h4l2 4v8H5a2 2 0 0 1-2-2Z"/><path d="M13 14V8a2 2 0 0 1 2-2h4l2 4v8h-6a2 2 0 0 1-2-2Z"/></>,
    Settings: <><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></>,
    Network: <><circle cx="12" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="19" r="2"/><path d="M12 7v3M9.6 17.4l1.8-3.6M14.4 17.4 12.6 13.8"/></>,
    Layers: <><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></>,
    GraduationCap: <><path d="M22 10v6"/><path d="M2 10l10-5 10 5-10 5z"/><path d="M6 12v5c0 2 3 3 6 3s6-1 6-3v-5"/></>,
    Newspaper: <><path d="M2 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v14H4a2 2 0 0 1-2-2Z"/><path d="M18 4h2a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2"/><path d="M6 8h8M6 12h8M6 16h5"/></>,
    Home: <><path d="M3 9 12 2l9 7v11a2 2 0 0 1-2 2h-4a2 2 0 0 1-2-2v-5a2 2 0 1 0-4 0v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></>,
    BarChart: <><line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></>,
    Filter: <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>,
    Edit: <><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="m18.5 2.5 3 3L12 15l-4 1 1-4 9.5-9.5z"/></>,
    Coffee: <><path d="M17 8h1a4 4 0 1 1 0 8h-1"/><path d="M3 8h14v9a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4Z"/><line x1="6" y1="2" x2="6" y2="4"/><line x1="10" y1="2" x2="10" y2="4"/><line x1="14" y1="2" x2="14" y2="4"/></>,
    AlertTriangle: <><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></>,
    Moon: <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z"/>,
    Bell: <><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></>,
    Command: <path d="M18 3a3 3 0 0 0-3 3v12a3 3 0 0 0 3 3 3 3 0 0 0 3-3 3 3 0 0 0-3-3H6a3 3 0 0 0-3 3 3 3 0 0 0 3 3 3 3 0 0 0 3-3V6a3 3 0 0 0-3-3 3 3 0 0 0-3 3 3 3 0 0 0 3 3h12a3 3 0 0 0 3-3 3 3 0 0 0-3-3z"/>,
    Eye: <><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></>,
    MessageCircle: <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>,
    GitFork: <><circle cx="12" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><path d="M18 9v2c0 .6-.4 1-1 1H7c-.6 0-1-.4-1-1V9"/><path d="M12 12v3"/></>,
    Table: <><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="9" y1="3" x2="9" y2="21"/><line x1="15" y1="3" x2="15" y2="21"/></>,
    Keyboard: <><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M6 14h12"/></>,
    ArrowUpRight: <><path d="M7 17 17 7M7 7h10v10"/></>,
    Loader: <><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></>,
  };
  return (
    <svg viewBox="0 0 24 24" {...stroke} className={className} aria-hidden="true">
      {paths[name] || <circle cx="12" cy="12" r="9"/>}
    </svg>
  );
}

// Card primitive
function Card({ className = "", children, ...rest }) {
  return <div {...rest} className={cn("rounded-lg border border-zinc-200 bg-white", className)}>{children}</div>;
}
function CardBody({ className = "", children }) {
  return <div className={cn("p-5", className)}>{children}</div>;
}
function CardTitle({ className = "", children }) {
  return <h3 className={cn("text-[15px] font-semibold tracking-tight text-zinc-900", className)}>{children}</h3>;
}

// Label / pill / kbd
function Pill({ tone = "neutral", className = "", children }) {
  const tones = {
    neutral: "bg-zinc-100 text-zinc-700 border-zinc-200",
    accent: "bg-[#0b3a8a] text-white border-[#0b3a8a]",
    accentSoft: "bg-[#eaf0fb] text-[#0b3a8a] border-[#cfdcf3]",
    success: "bg-emerald-50 text-emerald-800 border-emerald-200",
    warn: "bg-amber-50 text-amber-800 border-amber-200",
    danger: "bg-rose-50 text-rose-800 border-rose-200",
    serif: "bg-zinc-50 text-zinc-700 border-zinc-200 font-serif italic",
  };
  return <span className={cn("inline-flex items-center gap-1 rounded-full border px-2 py-[1px] text-[11px] font-medium", tones[tone], className)}>{children}</span>;
}
function Kbd({ children }) {
  return <kbd className="inline-flex items-center justify-center min-w-[1.4rem] h-5 px-1 text-[10px] font-mono font-medium border border-zinc-300 bg-zinc-50 rounded text-zinc-700">{children}</kbd>;
}

// Buttons
function Btn({ tone = "outline", size = "md", className = "", children, ...rest }) {
  const sizes = { sm: "h-7 text-xs px-2.5", md: "h-8 text-[13px] px-3", lg: "h-9 text-sm px-4" };
  const tones = {
    primary: "bg-[#0b3a8a] text-white hover:bg-[#0a3278] border border-[#0b3a8a]",
    outline: "bg-white text-zinc-800 hover:bg-zinc-50 border border-zinc-200",
    ghost: "bg-transparent text-zinc-700 hover:bg-zinc-100 border border-transparent",
    soft: "bg-[#eaf0fb] text-[#0b3a8a] hover:bg-[#dde6f7] border border-[#cfdcf3]",
    danger: "bg-white text-rose-700 hover:bg-rose-50 border border-rose-200",
  };
  return (
    <button {...rest} className={cn("inline-flex items-center gap-1.5 rounded-md font-medium transition-colors", sizes[size], tones[tone], className)}>
      {children}
    </button>
  );
}

// Counter — large tabular number with label
function Stat({ value, label, sub, tone = "neutral" }) {
  const v = typeof value === "number" ? value.toLocaleString() : value;
  return (
    <div className="flex flex-col">
      <span className={cn("font-mono text-2xl font-semibold tabular-nums tracking-tight", tone === "accent" && "text-[#0b3a8a]", tone === "neutral" && "text-zinc-900")}>{v}</span>
      <span className="text-[11px] uppercase tracking-wider text-zinc-500 font-medium">{label}</span>
      {sub && <span className="text-[11px] text-zinc-500 mt-0.5">{sub}</span>}
    </div>
  );
}

// Progress bar — slim
function Bar({ value, max = 100, tone = "accent", className = "" }) {
  const tones = { accent: "bg-[#0b3a8a]", success: "bg-emerald-600", warn: "bg-amber-500" };
  return (
    <div className={cn("h-1 w-full rounded-full bg-zinc-100 overflow-hidden", className)}>
      <div className={cn("h-full rounded-full", tones[tone])} style={{ width: `${(value / max) * 100}%` }} />
    </div>
  );
}

// Tiny "score breakdown" sparkline-stack
function ScoreStack({ breakdown }) {
  // breakdown: { embedding, llm, recency, graph } summing to ~score
  const total = Object.values(breakdown).reduce((a, b) => a + b, 0);
  const colors = { embedding: "#0b3a8a", llm: "#1d6fe0", recency: "#7aa3e8", graph: "#cdd9ee" };
  return (
    <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-zinc-100">
      {Object.entries(breakdown).map(([k, v]) => (
        <div key={k} style={{ width: `${(v / total) * 100}%`, background: colors[k] }} title={`${k}: ${v.toFixed(2)}`} />
      ))}
    </div>
  );
}

// Section heading — serif
function SerifH({ className = "", children }) {
  return <h2 className={cn("font-serif text-2xl tracking-tight text-zinc-900", className)}>{children}</h2>;
}

// Annotation pill (for design rationale notes)
function Note({ children }) {
  return (
    <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50/60 px-3 py-2 text-[12px] leading-relaxed text-amber-900">
      <Icon name="Edit" className="h-3.5 w-3.5 mt-0.5 shrink-0 opacity-70" />
      <span>{children}</span>
    </div>
  );
}

Object.assign(window, { cn, Icon, Card, CardBody, CardTitle, Pill, Kbd, Btn, Stat, Bar, ScoreStack, SerifH, Note });
