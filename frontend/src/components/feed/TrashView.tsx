import { FeedView } from './FeedView';

export function TrashView() {
  return (
    <div className="space-y-4">
      <div className="rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
        Papers in Trash will be kept until you delete them forever. Restore returns them to their previous location.
      </div>
      <FeedView surface="trash" />
    </div>
  );
}
