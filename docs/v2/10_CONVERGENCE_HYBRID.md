# 10 — Fiyat yakınsaması ve hibrit

Durum: **tasarım**. Mevcut yakınsama kodu `wide_paper.assess_convergence` korunuyor; V2 değişiklikleri Phase 4'te.

## Yakınsama

- **Karşılaştırma:** mark ya da son fiyat değil, iki borsada aynı anda uygulanabilir fiyat.
  - Defterli borsada: boyuta göre bid/ask VWAP.
  - Variational'da: zaman sınırlı RFQ kotası.
  - Veranta'da: vault / dinamik kota.
- **İstatistikler:** robust medyan / MAD / z, yarı ömür, kesişim epizotları, yapısal baz. Rejim ve seans ayrı ele alınır (RWA RTH; Arcus off-hours bantları). Mark, index ve oracle gecikmeleri hesaba katılır.
- **Fırsat değildir:**
  - bayat ya da uyuşmayan RWA fiyatı;
  - kimliği doğrulanmamış çift (QNT, ANTHROPIC);
  - açık spread'in tamamı. Spread'in tamamı kâr gösterilmez; çıkış fiyatı, maliyet ve risk payı düşülür.
- **Yanlılıklar:** look-ahead, survivorship ve top-N seçim yanlılığı walk-forward / out-of-sample ile ölçülür.
- **Mevcut kanıt (V2-0):** 2 temiz işlem, +$1,10; gerçekleşen beklenenin %62'si. Hüküm vermeye yetmez.

## Hibrit

Funding ve yakınsama ayrı bileşenlerde tahmin edilir. Aynı baz hareketi iki kez sayılmaz:
- Fiyat yakalama (convergence) baz değişimini;
- funding, ödeme akışını içerir.

Funding lehteyken yakınsama aleyhteyse net değer gösterilir; bu durum gizlenmez. İki stratejinin ufukları (funding gerçekleşmesi ile yakınsama kapanışı), taşıma maliyeti, çıkış likiditesi, teminat ve kuyruk riski birlikte değerlendirilir.

## Fırsat hunisi (Phase 4 kodu)

```
DISCOVERED → IDENTITY_VERIFIED → MULTI_VENUE → DATA_READY → EXECUTION_COST_VERIFIED
→ FUNDING_OR_CONVERGENCE_EVIDENCE_READY → ECONOMICALLY_POSITIVE → RISK_APPROVED
→ PAPER_ELIGIBLE → PAPER_OPENED → PAPER_CLOSED
```

İlk karar kodları:
- **Veri:** `SINGLE_VENUE_ONLY`, `IDENTITY_UNVERIFIED`, `DATA_STALE`, `BOOK_DESYNC`, `QUOTE_EXPIRED`, `FUNDING_SIGN_UNKNOWN`, `SETTLEMENT_EVIDENCE_MISSING`, `FEE_UNKNOWN`, `EXECUTION_MODEL_UNVERIFIED`, `RWA_SESSION_MISMATCH`, `VENUE_OFFLINE`, `RATE_LIMITED`.
- **Ekonomik ret:** `NET_NEGATIVE`, `INSUFFICIENT_LIQUIDITY`.
- **Risk:** `RISK_REJECT`.

Veri eksikliği ile ekonomik ret ayrı tutulur. Mevcut `wide_paper` gerekçe grupları ve `/api/candidates` sayfalaması bu kodlara eşlenecek.
