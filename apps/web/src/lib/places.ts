import type { NearbyPlace } from "@rag-ragre/contracts";

/*
 * Static nearby-amenity catalog for THE CAMELLIA, mirrored 1:1 from
 * db/seed/static_places.json so the MapPanel renders instantly before any SSE
 * `places` event. Live query results (SSE) supersede this when available.
 * Keep these records identical to the seed file (name, kinds, lat, lng,
 * distance_m, address, rating) — do not invent coordinates here.
 */
export const STATIC_PLACES: NearbyPlace[] = [
  { name: "Biển Mỹ Khê", kinds: ["beach", "tourist_attraction"], lat: 16.06038, lng: 108.24585, distance_m: 5096, address: "Võ Nguyên Giáp, Phước Mỹ, Sơn Trà, Đà Nẵng", rating: 4.6 },
  { name: "Vịnh Sơn Trà", kinds: ["tourist_attraction", "natural_feature"], lat: 16.125, lng: 108.255, distance_m: 2203, address: "Bán đảo Sơn Trà, Đà Nẵng", rating: 4.7 },
  { name: "Bán đảo Sơn Trà", kinds: ["park", "tourist_attraction"], lat: 16.12049, lng: 108.26471, distance_m: 1948, address: "Bán đảo Sơn Trà, Thọ Quang, Sơn Trà, Đà Nẵng", rating: 4.8 },
  { name: "Chùa Linh Ứng", kinds: ["place_of_worship", "tourist_attraction"], lat: 16.10014, lng: 108.27844, distance_m: 2483, address: "Bãi Bụt, Bán đảo Sơn Trà, Đà Nẵng", rating: 4.8 },
  { name: "Nhà Trưng bày Hoàng Sa", kinds: ["museum", "tourist_attraction"], lat: 16.09347, lng: 108.25121, distance_m: 1393, address: "Đường Hoàng Sa, Thọ Quang, Sơn Trà, Đà Nẵng", rating: 4.5 },
  { name: "Bảo tàng 3D Art In Paradise", kinds: ["museum", "tourist_attraction"], lat: 16.09538, lng: 108.24308, distance_m: 1743, address: "Lô C2-10 Trần Nhân Tông, Thọ Quang, Sơn Trà, Đà Nẵng", rating: 4.4 },
  { name: "UBND Quận Sơn Trà", kinds: ["local_government_office"], lat: 16.06084, lng: 108.23354, distance_m: 5476, address: "Đường Đông Giang, An Hải Bắc, Sơn Trà, Đà Nẵng", rating: 3.8 },
  { name: "THCS Lý Tự Trọng", kinds: ["school"], lat: 16.10126, lng: 108.24815, distance_m: 927, address: "Đường Nguyễn Phan Vinh, Thọ Quang, Sơn Trà, Đà Nẵng", rating: 4.0 },
  { name: "Trường Tiểu học Nguyễn Tri Phương", kinds: ["school"], lat: 16.10063, lng: 108.24903, distance_m: 884, address: "Đường Lê Tân Trung, Thọ Quang, Sơn Trà, Đà Nẵng", rating: 4.1 },
  { name: "Co.opmart Sơn Trà", kinds: ["supermarket", "shopping_mall"], lat: 16.09447, lng: 108.24273, distance_m: 1837, address: "Trần Nhân Tông, Thọ Quang, Sơn Trà, Đà Nẵng", rating: 4.2 },
  { name: "Chợ Mai", kinds: ["market"], lat: 16.10024, lng: 108.25255, distance_m: 652, address: "Đường Nguyễn Phan Vinh, Thọ Quang, Sơn Trà, Đà Nẵng", rating: 3.9 },
  { name: "Chợ Chiều", kinds: ["market"], lat: 16.09611, lng: 108.2459, distance_m: 1463, address: "Đường Ngô Quyền, Thọ Quang, Sơn Trà, Đà Nẵng", rating: 3.8 },
  { name: "Trung tâm Hành chính TP Đà Nẵng", kinds: ["local_government_office"], lat: 16.07708, lng: 108.22269, distance_m: 4721, address: "24 Trần Phú, Thạch Thang, Hải Châu, Đà Nẵng", rating: 4.0 },
  { name: "Bệnh viện Đà Nẵng", kinds: ["hospital"], lat: 16.07294, lng: 108.21546, distance_m: 5607, address: "124 Hải Phòng, Thạch Thang, Hải Châu, Đà Nẵng", rating: 4.0 },
  { name: "Bệnh viện Vinmec Đà Nẵng", kinds: ["hospital"], lat: 16.03875, lng: 108.21123, distance_m: 8791, address: "Đường 30 Tháng 4, Hoà Cường Bắc, Hải Châu, Đà Nẵng", rating: 4.3 },
  { name: "Bệnh viện Gia đình Đà Nẵng", kinds: ["hospital"], lat: 16.05312, lng: 108.20899, distance_m: 7652, address: "73 Nguyễn Hữu Thọ, Hoà Thuận Nam, Hải Châu, Đà Nẵng", rating: 4.1 },
  { name: "Ga Đà Nẵng", kinds: ["transit_station", "train_station"], lat: 16.0716, lng: 108.2093, distance_m: 6216, address: "791 Hải Phòng, Tam Thuận, Thanh Khê, Đà Nẵng", rating: 4.0 },
  { name: "Cầu Sông Hàn", kinds: ["tourist_attraction", "bridge"], lat: 16.07264, lng: 108.23014, distance_m: 4541, address: "Cầu Sông Hàn, Sơn Trà / Hải Châu, Đà Nẵng", rating: 4.6 },
  { name: "Cầu Rồng", kinds: ["tourist_attraction", "bridge"], lat: 16.06125, lng: 108.228, distance_m: 5719, address: "Đường Nguyễn Văn Linh / Võ Văn Kiệt, Đà Nẵng", rating: 4.6 },
  { name: "GO! Đà Nẵng", kinds: ["shopping_mall", "supermarket"], lat: 16.06671, lng: 108.21347, distance_m: 6227, address: "257 Hùng Vương, Vĩnh Trung, Thanh Khê, Đà Nẵng", rating: 4.2 },
  { name: "Đại học Đà Nẵng", kinds: ["university"], lat: 16.07096, lng: 108.21972, distance_m: 5418, address: "41 Lê Duẩn, Hải Châu 1, Hải Châu, Đà Nẵng", rating: 4.3 },
  { name: "Chợ Hàn", kinds: ["market", "tourist_attraction"], lat: 16.06826, lng: 108.22439, distance_m: 5304, address: "119 Trần Phú, Hải Châu 1, Hải Châu, Đà Nẵng", rating: 4.1 },
  { name: "Chợ Mân Thái", kinds: ["market"], lat: 16.08565, lng: 108.24337, distance_m: 2547, address: "Đường Ngô Quyền, Mân Thái, Sơn Trà, Đà Nẵng", rating: 3.9 },
  { name: "Sân bay quốc tế Đà Nẵng", kinds: ["airport"], lat: 16.0429, lng: 108.19839, distance_m: 9253, address: "Duy Tân, Hoà Thuận Tây, Hải Châu, Đà Nẵng", rating: 4.2 },
];
