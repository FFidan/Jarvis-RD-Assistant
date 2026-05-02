import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { fetchMyDay } from '@/lib/api';
import type { MyDayResponse } from '@/types';
import { SectionHeader } from './SectionHeader';

const COLORS = ['#2563eb', '#16a34a', '#9333ea'];

const formatDate = (iso: string | null) => {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
};

export function ProjectsSection() {
  const navigate = useNavigate();

  const { data } = useQuery<MyDayResponse>({
    queryKey: ['my-day'],
    queryFn: fetchMyDay,
    refetchInterval: 60_000,
  });

  const projects = (data?.project_pulse ?? []).slice(0, 3);

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
      />

      <div className="space-y-3">
        {projects.map((project, i) => {
          const pct =
            project.total_tasks > 0
              ? Math.round((project.done_tasks / project.total_tasks) * 100)
              : 0;

          return (
            <div key={project.id} className="grid grid-cols-[16px_1fr_auto] gap-3 items-start">
              {/* Status dot */}
              <div className="mt-[5px] flex-shrink-0">
                <div className="h-2 w-2 rounded-full bg-emerald-500" />
              </div>

              {/* Name + meta */}
              <div className="min-w-0">
                <button
                  type="button"
                  className="text-[14px] font-medium text-soft hover:text-[var(--ink-blue)] text-left leading-snug truncate w-full"
                  onClick={() => navigate('/projects', { state: { projectId: project.id } })}
                >
                  {project.name}
                </button>
                <p className="font-mono text-[10px] text-meta mt-0.5 truncate">
                  {project.next_milestone || '—'} · due {formatDate(project.next_milestone_deadline)}
                </p>
              </div>

              {/* Progress % + bar */}
              <div className="flex flex-col items-end flex-shrink-0">
                <span className="font-mono tabular-nums text-[12px] text-soft">
                  {pct}%
                </span>
                <div className="mt-1 h-1 w-20 rounded-full bg-zinc-100 dark:bg-zinc-800">
                  <div
                    className="h-1 rounded-full"
                    style={{
                      width: pct + '%',
                      backgroundColor: COLORS[i % COLORS.length],
                    }}
                  />
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
