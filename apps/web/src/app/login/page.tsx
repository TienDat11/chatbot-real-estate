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
      className="app-viewport-min-height"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
        background:
          "radial-gradient(1000px 480px at 15% -10%, rgba(201, 162, 75, 0.18), transparent 60%)," +
          "radial-gradient(900px 420px at 90% 110%, rgba(168, 80, 46, 0.16), transparent 55%)," +
          "linear-gradient(135deg, #0E2A47 0%, #141D2B 55%, #1B2737 100%)",
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
