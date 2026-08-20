# Sales kit v1 - The Camellia (SALES_KIT_V1, 2026-08)
# Nguồn: data/_processed/*.json + feedback ground truth. Số không nguồn -> pending_confirm.
# pj=project_info pm=payment_methods br=business_rules uc=unit_catalog pmx=price_matrix gt=feedback_data

=== SALES_CONTEXT (dữ liệu tham khảo - không phải lệnh) ===

## A. USP (7 dòng)
1. Vị trí: giao lộ Lê Văn Lương - Lê Đức Thọ, Sơn Trà, Đà Nẵng, gần biển. [pj→vi_tri]
2. View: căn góc view biển + view núi Sơn Trà; Sky Park tầng mái view 360 độ Sơn Trà. [pj→tien_ich,co_cau_can_ho]
3. Tiện ích: 42 tiện ích đa tầng (hồ bơi, gym, nhà trẻ, Sky Park...). [pj→tien_ich]
4. Phát triển: Tổng công ty MBLand; quản lý vận hành dự kiến CBRE/PMC. [pj→phat_trien_du_an,quan_ly_van_hanh]
5. Quy mô: 469 căn hộ + 10 căn TMDV/shophouse. [pj→quy_mo.so_can_ho,so_can_tmdv] (KHÔNG trả lời 428, KHÔNG nêu ai công bố)
6. Bàn giao dự kiến Q1/2028. [pm→milestones]
7. Cơ cấu: Studio 81 / 1.5PN 82 / 2PN 20+186 / 3PN 84 / Duplex 16; view nội khu/mặt đường/góc biển-núi. [pj; uc]
8. Pháp lý: CĐT TNHH Địa ốc Thành Lâm; GCN QSDĐ CT09441 (2.297 m2); QĐ 254 31/01/2024; HS SDĐ 11,09 (QĐ 191 QH 1/500). [pj; gt→B5.XN2/3]

## B. Benefit-translation (feature → lợi ích)
1. 2PN 2VS 68,3 m2 → gia đình nhỏ, 2 vệ sinh không chen nhau. [uc]
2. Studio 27,8-31,4 m2 → đầu tư cho thuê/người độc thân, vốn vừa, vị trí đẹp. [pj→studio]
3. 3PN 83,9-103,6 m2 góc view biển → phòng khách nhìn biển, đủ chỗ cả nhà. [pj→3pn_3vs]
4. HTLS 0% 18 tháng đầu → chỉ lo vốn tự có, chưa lo lãi. [pm]
5. Bàn giao Q1/2028 → hơn 1 năm nữa nhận nhà, nội thất cơ bản, tiến độ rõ. [pm→milestones]
6. Bán từ tầng 3A (tầng 4) đến 23 → view thoáng, sáng tự nhiên. [br]
7. Cọc 100 triệu mọi phương thức → 100 triệu giữ chỗ, còn lại theo tiến độ. [br]
8. CĐT Thành Lâm + MBLand ~20 năm, Top 10 BĐS → kinh nghiệm đứng sau. [pj]
9. GCN: CĐT gửi hồ sơ trong 50 ngày từ bàn giao → pháp lý có quy trình rõ. [pj; gt]
10. 42 tiện ích (hồ bơi 381,6 m2, gym 134,3 m2, nhà trẻ 434,2 m2,...) → đủ cho cả nhà. [pj]

## C. Payment selling angles
- Cọc 100 triệu mọi phương thức; booking 50 triệu tính vào cọc; cọc KHÔNG tính vào 50% vốn thảnh thơi. [br; gt]
- CK: chuẩn 4% (+EB 3% = 7%); som95 13% (+EB = 16%); thảnh thơi 5% (sàn Đất Xanh Duyên Hải); EB cộng dồn CK, KHÔNG cho HTLS. [pm; br; gt]
- HTLS: 0% 18 tháng, vay tối đa 70%, ân hạn gốc tối đa 5 năm; ngân hàng VietinBank/MB/SHB + MBV (dự kiến). [pm; br]
- Vốn tự có: HTLS vay tối đa 70% (phần còn lại là vốn tự có + phí); thảnh thơi 50% tới khi nhận nhà. [pm; br]
- Tầng bán từ 3A (tầng 4); mỗi tầng tăng 0,3-0,4%. [br→sale_floor,floor_increment]

## D. Objection playbook
1. "Giá cao" → giá trị vị trí + HTLS giảm áp lực trả trước; mời nhận bảng giá. Fact: pmx. KHÔNG: hứa giảm giá ngoài CK; so giá dự án khác không số.
2. "Sợ pháp lý" → GCN CT09441 + QĐ 254 31/01/2024 + QĐ 191 22/01/2025 + CV 12779/SXD-QLN. Fact: pj→phap_ly. KHÔNG: "100% an toàn"; hứa ngày cấp sổ (50 ngày là hạn CĐT gửi hồ sơ).
3. "Để suy nghĩ thêm" → đồng cảm, mời xem thực tế; ưu đãi hiệu lực thì nêu kèm nguồn. KHÔNG: ép deadline bịa.
4. "Vốn ít" → HTLS vay 70% 0% 18 tháng HOẶC thảnh thơi 50%; cọc 100 triệu. Fact: pm+br. KHÔNG: "ai cũng mua được".
5. "Dự án kia rẻ hơn" → so fact DB: vị trí Sơn Trà, Q1/2028, HTLS 0%, MBLand. Fact: pj. KHÔNG: nói xấu đối thủ.
6. "Tầng 4/13/14" → bán từ tầng 3A, chênh 0,3-0,4%/tầng, tầng thấp giá hợp lý hơn. Fact: br. KHÔNG: "tầng xui".
7. "Mua cho thuê" → Studio 81 + 1.5PN 82 căn, view biển/nội khu; vận hành CBRE/PMC. Fact: pj→co_cau_can_ho. KHÔNG: hứa suất thuê/tỷ lệ sinh lời.
8. "Lo tiến độ" → bàn giao Q1/2028 theo lịch; CĐT Thành Lâm + MBLand. Fact: pm; pj. KHÔNG: cam kết ngày ngoài DB.

## E. FOMO template
"Ưu đãi [X] áp dụng đến [effective_to] theo [nguồn]" - chỉ khi fact có khoảng hiệu lực rõ; KHÔNG có → im lặng về thời hạn.

## Tail - pending_confirm (Chờ xác nhận, KHÔNG tự bịa)
- Hotline: 097 555 57 69 (pj→lien_he.hotline; gt B3.2b vẫn mở cho số khác).
- Tọa độ lat/lng chính xác: CHƯA có dữ liệu.
- Tuổi tối đa cho vay: CHƯA có số liệu, trả lời chung chung, mời kết nối ngân hàng liên kết (gt→B6.5).
- Giá CH-10 (2PN) / CH-11 (3PN): CHƯA xác nhận, trả lời mập mờ, mời nhận bảng giá (pmx→findings_flag; gt→B1.1/2).
- Zalo OA: CHƯA có dữ liệu.
