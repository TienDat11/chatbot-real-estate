import { AuthProvider } from "@/lib/AuthProvider";
import { RegisterScreen } from "@/features/auth/RegisterScreen";

export default function RegisterPage() {
  return (
    <AuthProvider>
      <RegisterScreen />
    </AuthProvider>
  );
}
