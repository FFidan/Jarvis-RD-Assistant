import { useNavigate } from 'react-router-dom';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { useAuthStore } from '@/stores/auth-store';

export function UserAvatarMenu() {
  const navigate = useNavigate();
  // auth-store has no user/email field — only apiKey. Use first char of apiKey
  // as the avatar initial, falling back to 'J' for JARVIS.
  const apiKey = useAuthStore((s) => s.apiKey);
  const logout = useAuthStore((s) => s.logout);
  const initial = apiKey ? apiKey.charAt(0).toUpperCase() : 'J';

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className="h-8 w-8 rounded-full bg-[var(--ink-blue)] text-white font-mono text-[11px] flex items-center justify-center hover:opacity-90 transition-opacity"
          aria-label="User menu"
        >
          {initial}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onClick={() => navigate('/settings')}>
          Settings
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => { logout(); navigate('/'); }}>
          Logout
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
