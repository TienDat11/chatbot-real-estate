import { Suspense } from "react";
import { RootAuthGate } from "@/components/RootAuthGate";
import { AuthProvider } from "@/lib/AuthProvider";

/** The bare root is always an explicit login/guest gate, never an automatic chat. */
export default function Home() {
  return (
    <main className="app-viewport-min-height root-auth-shell">
      <AuthProvider>
        <Suspense fallback={null}>
          <RootAuthGate />
        </Suspense>
      </AuthProvider>
    </main>
  );
}
