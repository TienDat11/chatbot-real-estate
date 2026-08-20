import { redirect } from "next/navigation";

// Legacy route from before the map list mode moved into the single chat page.
// Redirect to the root so an old bookmark never mounts a second ChatPage
// (which would lose chat state via a fresh mount). The map panel is always
// visible, so no ?tab= param is needed; ?mode=list is handled on the root.
export default function ListMapsPage() {
  redirect("/?mode=list");
}
