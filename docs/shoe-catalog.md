# Shoe catalog and assignment

The selector supports searchable brand and model suggestions, optional color text, and custom entries without a gender field. Clicking "Use this shoe" copies the selection into the editable personal shoe form. Color is stored in the shoe name, so existing storage and inference remain compatible. Selecting a model without a photo clears the previous photo instead of incorrectly reusing it.

The current starter directory contains 25 models across seven brands. Additional model names were checked against the public RunRepeat running-shoe catalog on 2026-09-14; this is a curated snapshot, not a live API or an exhaustive market catalog. No reviews or lab photos were imported. RunRepeat advertises a licensed retailer REST API and batch exports; automated catalog synchronization is not enabled.

Existing photos are representative images and may differ from the requested color. Missing or broken photos use a locally rendered vector shoe illustration labeled as an illustration, not a product photo. This fallback requires no image-generation API, network request, or usage charge. Users can supply their own image URL. Photo upload and automatic image search are not implemented.

The bundled catalog is a small, curated starting point, not a complete shoe database. Each entry records the exact variant, original product page, remote image URL, and attribution. Images remain hosted by the original provider and may change or become unavailable. Product photography belongs to its respective owner; linking a catalog image does not grant redistribution rights. Users can enter their own shoe model and image URL.

[KicksDB](https://kicks.dev/) advertises a free Standard API for sneaker catalogs, including product lookup and images. Its [documentation](https://docs.kicks.dev/guides) requires an account and API key. Running-shoe coverage and permitted image use should be checked before enabling this provider; it is not connected in this build. The bundled entries require no API key.

Purchase date limits eligibility for inferred assignments. Distance, pace, run type, and priority rules rank eligible shoes. Inference is a suggestion, not evidence of what the runner wore. Manual assignments, including an explicit unassignment, must be preserved on later inference and provider sync. Users should review proposed changes before applying them to historical runs.
