import { Link } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import type { ProjectPulseItem } from '@/types';

interface ProjectPulseProps {
  projects: ProjectPulseItem[];
}

export function ProjectPulse({ projects }: ProjectPulseProps) {
  if (projects.length === 0) {
    return null;
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-lg">Project Pulse</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {projects.map((project) => {
          const pct = project.total_tasks > 0
            ? Math.round((project.done_tasks / project.total_tasks) * 100)
            : 0;

          return (
            <div key={project.id} className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Link
                  to="/projects"
                  className="text-sm font-medium hover:underline"
                >
                  {project.name}
                </Link>
                <span className="text-sm text-muted-foreground">{pct}%</span>
              </div>
              <Progress value={pct} className="h-2" />
              {project.next_milestone && (
                <p className="text-xs text-muted-foreground">
                  Next: {project.next_milestone}
                  {project.next_milestone_deadline && (
                    <span className="ml-1">
                      · {new Date(project.next_milestone_deadline).toLocaleDateString()}
                    </span>
                  )}
                </p>
              )}
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
