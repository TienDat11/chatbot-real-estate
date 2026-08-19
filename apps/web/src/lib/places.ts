import type { NearbyPlace } from "@rag-ragre/contracts";

/*
 * Static nearby-amenity catalog for THE CAMELLIA (db/seed/static_places.json),
 * mirrored so the MapPanel can render instantly before any SSE `places` event.
 * Live query results (SSE) supersede this when available. Approx coords, rumor-grade.
 */
export const STATIC_PLACES: NearbyPlace[] = [
  { name: "Biển Mỹ Khê", kinds: ["beach", "tourist_attraction"], lat: 16.0603, lng: 108.2489, distance_m: 900, address: "Phường Phước Mỹ, Sơn Trà, Đà Nẵng", rating: 4.6 },
  { name: "Vịnh Sơn Trà", kinds: ["tourist_attraction", "natural_feature"], lat: 16.08, lng: 108.26, distance_m: 3000, address: "Bán đảo Sơn Trà, Đà Nẵng", rating: 4.7 },
  { name: "Bán đảo Sơn Trà", kinds: ["park", "tourist_attraction"], lat: 16.093, lng: 108.278, distance_m: 4500, address: "Sơn Trà, Đà Nẵng", rating: 4.8 },
  { name: "Chùa Linh Ứng", kinds: ["place_of_worship", "tourist_attraction"], lat: 16.1, lng: 108.273, distance_m: 5500, address: "Bán đảo Sơn Trà, Đà Nẵng", rating: 4.8 },
  { name: "Nhà Trưng bày Hoàng Sa", kinds: ["museum", "tourist_attraction"], lat: 16.07, lng: 108.235, distance_m: 1800, address: "Sơn Trà, Đà Nẵng", rating: 4.5 },
  { name: "Bảo tàng 3D Art In Paradise", kinds: ["museum", "tourist_attraction"], lat: 16.052, lng: 108.23, distance_m: 2500, address: "Sơn Trà, Đà Nẵng", rating: 4.4 },
  { name: "UBND phường Sơn Trà", kinds: ["local_government_office"], lat: 16.068, lng: 108.238, distance_m: 1500, address: "Sơn Trà, Đà Nẵng", rating: 3.8 },
  { name: "THCS Lý Tự Trọng", kinds: ["school"], lat: 16.06, lng: 108.24, distance_m: 700, address: "Sơn Trà, Đà Nẵng", rating: 4.0 },
  { name: "Trường Tiểu học Nguyễn Tri Phương", kinds: ["school"], lat: 16.062, lng: 108.242, distance_m: 800, address: "Sơn Trà, Đà Nẵng", rating: 4.1 },
  { name: "Co.opmart Sơn Trà", kinds: ["supermarket", "shopping_mall"], lat: 16.059, lng: 108.239, distance_m: 800, address: "Sơn Trà, Đà Nẵng", rating: 4.2 },
  { name: "Chợ Mai", kinds: ["market"], lat: 16.058, lng: 108.241, distance_m: 600, address: "Sơn Trà, Đà Nẵng", rating: 3.9 },
  { name: "Chợ Chiều", kinds: ["market"], lat: 16.057, lng: 108.244, distance_m: 400, address: "Sơn Trà, Đà Nẵng", rating: 3.8 },
  { name: "Trung tâm Hành chính TP Đà Nẵng", kinds: ["local_government_office"], lat: 16.054, lng: 108.22, distance_m: 2800, address: "Hải Châu, Đà Nẵng", rating: 4.0 },
  { name: "Bệnh viện Đà Nẵng", kinds: ["hospital"], lat: 16.077, lng: 108.221, distance_m: 3500, address: "Hải Châu, Đà Nẵng", rating: 4.0 },
  { name: "Bệnh viện Vinmec Đà Nẵng", kinds: ["hospital"], lat: 16.048, lng: 108.224, distance_m: 3000, address: "Ngũ Hành Sơn, Đà Nẵng", rating: 4.3 },
  { name: "Bệnh viện Gia đình Đà Nẵng", kinds: ["hospital"], lat: 16.05, lng: 108.223, distance_m: 3100, address: "Hải Châu, Đà Nẵng", rating: 4.1 },
  { name: "Ga Đà Nẵng", kinds: ["transit_station", "train_station"], lat: 16.077, lng: 108.213, distance_m: 4200, address: "Thanh Khê, Đà Nẵng", rating: 4.0 },
  { name: "Cầu Sông Hàn", kinds: ["tourist_attraction", "bridge"], lat: 16.062, lng: 108.227, distance_m: 2200, address: "Sông Hàn, Đà Nẵng", rating: 4.6 },
  { name: "Cầu Rồng", kinds: ["tourist_attraction", "bridge"], lat: 16.061, lng: 108.228, distance_m: 2100, address: "Sông Hàn, Đà Nẵng", rating: 4.6 },
  { name: "GO! Đà Nẵng", kinds: ["shopping_mall", "supermarket"], lat: 16.052, lng: 108.225, distance_m: 2600, address: "Hải Châu, Đà Nẵng", rating: 4.2 },
  { name: "Đại học Đà Nẵng", kinds: ["university"], lat: 16.071, lng: 108.226, distance_m: 2500, address: "Hải Châu, Đà Nẵng", rating: 4.3 },
  { name: "Chợ Hàn", kinds: ["market", "tourist_attraction"], lat: 16.069, lng: 108.226, distance_m: 2400, address: "Hải Châu, Đà Nẵng", rating: 4.1 },
  { name: "Chợ Mân Thái", kinds: ["market"], lat: 16.07, lng: 108.233, distance_m: 1800, address: "Sơn Trà, Đà Nẵng", rating: 3.9 },
  { name: "Sân bay quốc tế Đà Nẵng", kinds: ["airport"], lat: 16.044, lng: 108.199, distance_m: 5500, address: "Hải Châu, Đà Nẵng", rating: 4.2 },
];