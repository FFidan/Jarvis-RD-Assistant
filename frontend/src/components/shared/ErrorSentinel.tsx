type Props = {
  message: string;
  className?: string;
};

export function ErrorSentinel({ message, className }: Props) {
  return (
    <p role="status" className={`text-xs text-destructive pl-1 ${className ?? ""}`.trim()}>
      {message}
    </p>
  );
}
