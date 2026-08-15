import { ReactNode, useEffect } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "@/store/auth";
import type { UserRole } from "@/types";

interface Props {
  children: ReactNode;
  role?: UserRole;
}

export function ProtectedRoute({ children, role }: Props) {
  const token = useAuth((s) => s.token);
  const user = useAuth((s) => s.user);
  const fetchMe = useAuth((s) => s.fetchMe);

  useEffect(() => {
    if (token && !user) {
      void fetchMe();
    }
  }, [token, user, fetchMe]);

  if (!token) return <Navigate to="/login" replace />;
  if (role && user && user.role !== role) {
    return <Navigate to="/inbox" replace />;
  }
  return <>{children}</>;
}
