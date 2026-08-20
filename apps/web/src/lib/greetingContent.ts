import type { Image, Video } from "@rag-ragre/contracts";

/**
 * Static first-open greeting content. The intro (text, gallery, video hero) is
 * intentionally hardcoded here instead of being fetched from the backend: we
 * want the opening message to render instantly with no network dependency, and
 * later copy or asset changes live in this one file (edit -> build -> deploy).
 */

// Short sales nudge shown between the film hero and the image gallery. Lives
// in the static config so copy tweaks stay edit -> build -> deploy.
export const GREETING_MID_TEXT =
  "Anh/Chị đang tìm căn để ở, đầu tư hay cho thuê? Xem qua thước phim và hình ảnh dưới đây, " +
  "rồi nhắn nhu cầu của mình, em sẵn sàng tư vấn chi tiết từng căn phù hợp nhất.";

// Sales tone, aspirational but grounded in The Camellia's selling points.
export const GREETING_STATIC_TEXT =
  "Kính chào Anh/Chị! Em là chuyên viên tư vấn The Camellia Sơn Trà, Đà Nẵng. " +
  "Dự án tọa lạc ngay giao lộ Lê Văn Lương - Lê Đức Thọ, phường Thọ Quang, quận Sơn Trà - " +
  "vừa sát biển Mỹ Khê vừa ngược view núi Sơn Trà hùng vĩ, cùng 42 tiện ích đa tầng cho trọn đời sống gia đình. " +
  "Anh/Chị đang tìm căn để ở, đầu tư, cho thuê, hay mở văn phòng/khách sạn ạ? " +
  "Cứ nhắn nhu cầu, em sẽ tư vấn tận tình và gợi ý căn phù hợp nhất để Anh/Chị an tâm sở hữu nhé.";

const CDN_ROOT = "https://pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev";

// Gallery images shown inside the greeting. kind=matbang to keep the project
// source metadata, with sales-toned alt text for relevance + accessibility.
export const GREETING_IMAGES: Image[] = [
  {
    image_id: "matbang-01",
    kind: "matbang",
    title: "Mặt bằng tổng thể",
    caption: "Mặt bằng tổng thể The Camellia Sơn Trà",
    alt_text: "Mặt bằng tổng thể dự án The Camellia Sơn Trà với không gian biển và núi",
    url_cdn: `${CDN_ROOT}/images/matbang/matbang-01.png`,
    width: null,
    height: null,
    score: 1,
  },
  {
    image_id: "matbang-02",
    kind: "matbang",
    title: "Phối cảnh render",
    caption: "Phối cảnh render toàn khu",
    alt_text: "Phối cảnh render toàn khu căn hộ The Camellia Sơn Trà",
    url_cdn: `${CDN_ROOT}/images/matbang/matbang-02.png`,
    width: null,
    height: null,
    score: 1,
  },
  {
    image_id: "matbang-03",
    kind: "matbang",
    title: "Bản đồ 42 tiện ích",
    caption: "Bản đồ 42 tiện ích nội khu",
    alt_text: "Bản đồ hệ thống 42 tiện ích nội khu The Camellia Sơn Trà",
    url_cdn: `${CDN_ROOT}/images/matbang/matbang-03.png`,
    width: null,
    height: null,
    score: 1,
  },
  {
    image_id: "matbang-04",
    kind: "matbang",
    title: "Collage tiện ích",
    caption: "Collage tiện ích đa tầng",
    alt_text: "Collage các tiện ích đa tầng tại The Camellia Sơn Trà gồm hồ bơi, gym, lounge",
    url_cdn: `${CDN_ROOT}/images/matbang/matbang-04.png`,
    width: null,
    height: null,
    score: 1,
  },
];

const BRAND_POSTER = `${CDN_ROOT}/images/matbang/matbang-02.png`;

// Hero video tapes. Exactly one per kind (brand + drone) so the tab picker never
// shows a duplicated label. The web build is preferred over an "original" master
// for the brand film.
export const GREETING_VIDEOS: Video[] = [
  {
    title: "The Camellia - Brand Film",
    url_cdn: `${CDN_ROOT}/media/video/brand-film-web.mp4`,
    kind: "brand",
    poster_url: BRAND_POSTER,
  },
  {
    title: "The Camellia - Bay quay tổng quan",
    url_cdn: `${CDN_ROOT}/media/video/dji-orbit-faststart.mp4`,
    kind: "drone",
    poster_url: BRAND_POSTER,
  },
];
