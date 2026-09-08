/**
 * Strict reader for the Firebase web-app configuration.
 *
 * Reads every NEXT_PUBLIC_FIREBASE_* variable and, when ANY of them is missing,
 * throws ONE error listing ALL missing names so a misconfigured deploy fails
 * fast at startup instead of silently pointing the browser at the wrong
 * project. All six values are public (they ship to the browser); provision
 * them in apps/web/.env.local.
 */
export interface FirebaseEnvironmentConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  storageBucket: string;
  messagingSenderId: string;
  appId: string;
}

/** Every NEXT_PUBLIC_FIREBASE_* variable the web app needs. */
export const FIREBASE_ENVIRONMENT_VARIABLE_NAMES = [
  "NEXT_PUBLIC_FIREBASE_API_KEY",
  "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN",
  "NEXT_PUBLIC_FIREBASE_PROJECT_ID",
  "NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET",
  "NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID",
  "NEXT_PUBLIC_FIREBASE_APP_ID",
] as const;

/**
 * Static `process.env.NEXT_PUBLIC_*` member reads: bundlers (Turbopack/
 * webpack) only inline NEXT_PUBLIC_* into the client bundle for literal
 * member expressions, so dynamic `process.env[variableName]` lookups would
 * always see an empty object in the browser.
 */
function readProcessFirebaseEnvironment(): Record<string, string | undefined> {
  return {
    NEXT_PUBLIC_FIREBASE_API_KEY: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
    NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
    NEXT_PUBLIC_FIREBASE_PROJECT_ID: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
    NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET: process.env.NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET,
    NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID:
      process.env.NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID,
    NEXT_PUBLIC_FIREBASE_APP_ID: process.env.NEXT_PUBLIC_FIREBASE_APP_ID,
  };
}

/**
 * @param environment env record to read; injectable so unit tests can verify
 *   the missing-variable error without touching the real process env.
 */
export function readFirebaseEnvironmentConfig(
  environment: Record<string, string | undefined> = readProcessFirebaseEnvironment()
): FirebaseEnvironmentConfig {
  const missingVariableNames = FIREBASE_ENVIRONMENT_VARIABLE_NAMES.filter(
    (variableName) => !environment[variableName]
  );
  if (missingVariableNames.length > 0) {
    throw new Error(
      `Missing Firebase environment variables: ${missingVariableNames.join(
        ", "
      )}. ` +
        "Provision all NEXT_PUBLIC_FIREBASE_* values in apps/web/.env.local " +
        "before starting the app."
    );
  }
  return {
    apiKey: environment.NEXT_PUBLIC_FIREBASE_API_KEY,
    authDomain: environment.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
    projectId: environment.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
    storageBucket: environment.NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET,
    messagingSenderId: environment.NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID,
    appId: environment.NEXT_PUBLIC_FIREBASE_APP_ID,
  } as FirebaseEnvironmentConfig;
}
