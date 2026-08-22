import { Suspense } from "react";
import { AuthProvider } from "@/lib/AuthProvider";
import { LoginScreen } from "@/features/auth/LoginScreen";

/**
 * Login route (story 8.3). Suspense boundary is required because LoginScreen
 * reads the `next` search param via useSearchParams during prerender.
 */
export default function LoginPage() {
  return (
    <main
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
      }}
    >
      <AuthProvider>
        <Suspense fallback={null}>
          <LoginScreen />
        </Suspense>
      </AuthProvider>
    </main>
  );
}
