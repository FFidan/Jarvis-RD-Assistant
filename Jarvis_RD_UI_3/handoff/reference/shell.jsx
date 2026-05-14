// Sidebar + topbar wrapper used by every artboard so each variant is shown in real product context

const NAV = [
  { path: "/", label: "Home", icon: "Home" },
  { path: "/my-day", label: "My Day", icon: "Sun", active: true },
  { path: "/pulse", label: "Pulse Deck", icon: "Sparkles" },
  { path: "/feed", label: "Research Feed", icon: "Newspaper" },
  { path: "/ask", label: "Ask", icon: "MessageCircle" },
  { path: "/analytics", label: "Analytics", icon: "BarChart" },
  { path: "/projects", label: "Projects", icon: "Folder" },
  { path: "/cards", label: "Learning Cards", icon: "GraduationCap" },
  { path: "/settings", label: "Settings", icon: "Settings" },
  { path: "/citations", label: "Citation Graph", icon: "GitFork" },
  { path: "/knowledge", label: "Knowledge Graph", icon: "Network" },
  { path: "/extractions", label: "Extraction Table", icon: "Table" },
];

function AppShell({ children, narrow = false, accent = "ink" }) {
  // accent: "ink" (new) or "slate" (current/baseline)
  const accentClass = accent === "ink" ? "[&_.accent]:text-[#0b3a8a]" : "[&_.accent]:text-zinc-900";
  return (
    <div className={cn("flex h-full bg-zinc-50/60", accentClass)}>
      {/* Sidebar */}
      <aside className="w-56 shrink-0 border-r border-zinc-200 bg-white flex flex-col">
        <div className="h-12 px-4 flex items-center border-b border-zinc-200">
          <span className={cn("font-serif text-[17px] tracking-tight font-semibold", accent === "ink" ? "text-[#0b3a8a]" : "text-zinc-900")}>JARVIS</span>
          <span className="ml-2 text-[10px] text-zinc-400 uppercase tracking-wider">R&D</span>
        </div>
        <nav className="flex-1 p-2 space-y-0.5">
          {NAV.map(n => (
            <a key={n.path} href="#" onClick={e => e.preventDefault()} className={cn(
              "flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-[13px] transition-colors",
              n.active ? (accent === "ink" ? "bg-[#0b3a8a] text-white" : "bg-zinc-900 text-white")
                       : "text-zinc-600 hover:bg-zinc-100"
            )}>
              <Icon name={n.icon} className="h-3.5 w-3.5" />
              <span>{n.label}</span>
            </a>
          ))}
        </nav>
        <div className="border-t border-zinc-200 p-3 space-y-1.5">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-medium mb-1.5">Services</div>
          {[["Paper Ingestion", "ok"], ["Pulse Runner", "ok"], ["LiteLLM router", "ok"], ["Learning Engine", "ok"]].map(([n, s]) => (
            <div key={n} className="flex items-center gap-2 text-[11px] text-zinc-600">
              <span className={cn("h-1.5 w-1.5 rounded-full", s === "ok" ? "bg-emerald-500" : "bg-rose-500")} />
              <span>{n}</span>
            </div>
          ))}
        </div>
      </aside>

      {/* Main */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Topbar */}
        <header className="h-12 border-b border-zinc-200 bg-white flex items-center px-4 gap-3 shrink-0">
          <span className={cn("text-[13px] font-medium", accent === "ink" ? "text-zinc-900" : "text-zinc-900")}>My Day</span>
          <div className="flex-1 max-w-md ml-4 relative">
            <Icon name="Search" className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-zinc-400" />
            <input type="text" placeholder="Search papers, notes, cards…" className="w-full h-7 pl-8 pr-12 text-[12px] border border-zinc-200 rounded-md bg-zinc-50 focus:outline-none focus:bg-white focus:border-zinc-300" />
            <span className="absolute right-2 top-1/2 -translate-y-1/2 flex gap-0.5">
              <Kbd>⌘</Kbd><Kbd>K</Kbd>
            </span>
          </div>
          <div className="ml-auto flex items-center gap-2 text-[11px] text-zinc-500">
            {/* Header Pomodoro chip — mirrors HeaderPomodoro.tsx */}
            <button className="h-7 inline-flex items-center gap-1.5 px-2 rounded-md border border-zinc-200 bg-zinc-50 hover:bg-white">
              <Icon name="Clock" className="h-3 w-3 text-[#0b3a8a]"/>
              <span className="font-mono tabular-nums text-[11px] font-medium text-zinc-900">23:48</span>
              <span className="text-[10px] text-zinc-500 max-w-[120px] truncate">Reread Kidger §4</span>
            </button>
            <button className="h-7 inline-flex items-center gap-1 px-2 rounded-md hover:bg-zinc-100" title="Background jobs">
              <Icon name="Activity" className="h-3 w-3"/>
              <span className="font-mono tabular-nums text-[11px]">2</span>
            </button>
            <button className="h-7 w-7 grid place-items-center rounded hover:bg-zinc-100" title="Keyboard shortcuts (?)">
              <Icon name="Keyboard" className="h-3.5 w-3.5"/>
            </button>
            <div className={cn("h-6 w-6 rounded-full grid place-items-center text-white text-[10px] font-semibold ml-1", accent === "ink" ? "bg-[#0b3a8a]" : "bg-zinc-700")}>F</div>
          </div>
        </header>

        {/* Body */}
        <div className={cn("flex-1 overflow-auto bg-zinc-50/60", narrow ? "" : "")}>
          {children}
        </div>
      </div>
    </div>
  );
}

window.AppShell = AppShell;
window.NAV = NAV;
