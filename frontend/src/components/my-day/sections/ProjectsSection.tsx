import { useNavigate, Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchMyDay } from '@/lib/api';
import type { MyDayResponse } from '@/types';
import { MarkerCaption as SectionHeader } from '@/components/typography/MarkerCaption';
import { GradientProgressBar } from '@/components/my-day/primitives/GradientProgressBar';

const COLOR_TOKENS = ['var(--project-1)', 'var(--project-2)', 'var(--project-3)'] as const;

const formatDate = (iso: string | null) => {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
};

export function ProjectsSection() {
  const navigate = useNavigate();

  const { data, isError } = useQuery<MyDayResponse>({
    queryKey: QUERY_KEYS.myDay.today(),
    queryFn: fetchMyDay,
    refetchInterval: 60_000,
  });

  const projects = (data?.project_pulse ?? []).slice(0, 3);

  if (isError) {
    return (
      <section id="projects">
        <SectionHeader marker="Projects" />
        <p className="text-[12px] font-mono text-meta pl-5">Couldn't load projects</p>
      </section>
    );
  }

  if (!data || data.project_pulse.length === 0) {
    return (
      <section id="projects">
        <SectionHeader marker="Projects" meta="none active" />
      </section>
    );
  }

  return (
    <section id="projects">
      <SectionHeader
        marker="Projects"
        meta={`${data.project_pulse.length} active`}
        right={<Link to="/projects" className="text-meta uppercase tracking-[0.18em] text-soft hover:text-strong">all projects →</Link>}
      />

      <div className="space-y-4">
        {projects.map((project, i) => {
          const pct =
            project.total_tasks > 0
              ? Math.round((project.done_tasks / project.total_tasks) * 100)
              : 0;

          const daysUntilDeadline = project.next_milestone_deadline
            ? Math.floor(
                (new Date(project.next_milestone_deadline).getTime() - Date.now()) / 86400000
              )
            : null;
          const completionRatio = project.total_tasks > 0
            ? project.done_tasks / project.total_tasks
            : 1;
          const isAtRisk =
            daysUntilDeadline !== null && daysUntilDeadline <= 7 && completionRatio < 0.5;
          const dotClass = isAtRisk ? 'bg-amber-500' : 'bg-emerald-500';
          // Use project.color if set; fall back to rotating palette for gradient bar
          const projectColor = project.color ?? `hsl(${COLOR_TOKENS[i % COLOR_TOKENS.length]})`;

          return (
            <div key={project.id} className="space-y-1.5">
              <div className="grid grid-cols-[16px_1fr_auto] gap-3 items-center">
                {/* Status dot */}
                <div className="shrink-0">
                  <div className={`h-2 w-2 rounded-full ${dotClass}`} />
                </div>

                {/* Name */}
                <button
                  type="button"
                  className="text-[14px] font-medium text-soft hover:text-[var(--ink-blue)] text-left leading-snug truncate w-full flex items-center gap-1.5"
                  onClick={() => navigate('/projects', { state: { projectId: project.id } })}
                >
                  {project.color && (
                    <span
                      className="w-2 h-2 rounded-full inline-block shrink-0"
                      style={{ backgroundColor: project.color }}
                    />
                  )}
                  {project.name}
                </button>

                {/* Progress % */}
                <span className="font-mono tabular-nums text-[10.5px] text-meta shrink-0">
                  {pct}%
                </span>
              </div>

              {/* Gradient progress bar — full width of card (skips the 16px dot column) */}
              <div className="pl-5">
                <GradientProgressBar value={pct} color={projectColor} />
              </div>

              {/* Milestone + due date below bar */}
              <p className="pl-5 font-mono text-[10px] text-meta truncate">
                {project.next_milestone || '—'} · due {formatDate(project.next_milestone_deadline)}
              </p>
            </div>
          );
        })}
      </div>
    </section>
  );
}
