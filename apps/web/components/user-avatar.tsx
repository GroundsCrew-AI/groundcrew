import { cn } from "cn";

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";

export function UserAvatar({
  src,
  name,
  className,
}: {
  src: string | null | undefined;
  name: string | null | undefined;
  className?: string;
}) {
  const initials = (name ?? "?")
    .split(/\s+/)
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  return (
    <Avatar className={cn("size-5", className)}>
      {src && <AvatarImage src={src} alt="" />}
      <AvatarFallback className="text-[9px]">{initials}</AvatarFallback>
    </Avatar>
  );
}
