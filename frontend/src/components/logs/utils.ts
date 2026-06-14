/** Shared colour map for log levels. */
export const LEVEL_COLORS: Record<string, string> = {
  debug: '#6b7280',
  info: '#3b82f6',
  warning: '#f59e0b',
  error: '#ef4444',
  critical: '#7c3aed',
};

export const LEVEL_BADGE_CLASSES: Record<string, string> = {
  debug: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300',
  info: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
  warning: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-300',
  error: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
  critical: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300',
};

export const CATEGORY_BADGE_CLASSES: Record<string, string> = {
  error: 'border border-red-400 text-red-600 dark:border-red-500 dark:text-red-400',
  job: 'bg-blue-50 text-blue-600 dark:bg-blue-900/20 dark:text-blue-400',
  source: 'bg-green-50 text-green-600 dark:bg-green-900/20 dark:text-green-400',
  auth: 'bg-orange-50 text-orange-600 dark:bg-orange-900/20 dark:text-orange-400',
  config: 'bg-indigo-50 text-indigo-600 dark:bg-indigo-900/20 dark:text-indigo-400',
  infra: 'bg-slate-50 text-slate-600 dark:bg-slate-900/20 dark:text-slate-400',
};
