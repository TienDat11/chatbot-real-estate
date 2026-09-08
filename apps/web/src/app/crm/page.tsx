import { redirect } from "next/navigation";

/** Keep the legacy CRM URL compatible with the canonical sales board. */
export default function CrmCompatibilityPage() {
  redirect("/sales/leads");
}
