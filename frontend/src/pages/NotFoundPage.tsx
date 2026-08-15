import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/button';

export function NotFoundPage() {
  return (
    <div className="flex min-h-[50vh] flex-col items-center justify-center gap-4">
      <h1 className="text-6xl font-bold text-muted-foreground">404</h1>
      <p className="text-xl text-muted-foreground">Page not found</p>
      <div className="flex flex-wrap justify-center gap-2">
        <Button asChild><Link to="/">Go Home</Link></Button>
        <Button asChild variant="outline"><Link to="/feed?surface=library">Open Papers</Link></Button>
        <Button asChild variant="outline"><Link to="/feed?surface=search">Open Discover</Link></Button>
      </div>
    </div>
  );
}
