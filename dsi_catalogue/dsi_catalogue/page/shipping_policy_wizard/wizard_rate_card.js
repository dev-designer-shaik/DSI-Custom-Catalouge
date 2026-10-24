// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The rate card's builders and words, as plain data and plain text: what the Rates step builds and shows, and
// what the Review step says of the card (shipping_rate_card.py documents the card). Exported on dsi_catalogue.sp:
//	sp.card_as_kept(card)       the card with its keys in the order the ERP keeps them: sorted, as the card's JSON
//	                            is written (shipping_policy_data._card_text), and so wizard_state hands a saved
//	                            card out. A card taken from an import then equals the same card saved, key order
//	                            included, so the draft's dirty stays exact
//	sp.new_card_profile(name), sp.new_card_zone(name), sp.new_card_tier(from, to, price)
//	                            a profile, a zone (one price at any weight, not typed yet) and a tier, keys in
//	                            that same order
//	sp.card_profiles(card), sp.card_zones(profile), sp.card_tiers(zone)   the entries that are records
//	sp.card_counts(card)        {profiles, zones, tiers, countries}, counted as shipping_rate_card.card_summary
//	                            counts them (a country once, however many zones list it)
//	sp.counts_words(counts)     "4 profiles, 14 zones, 49 weight tiers and 232 countries"
//	sp.is_flat_zone(zone)       one price at any weight: a single tier with neither weight
//	sp.pounds(value)            a weight as the card's sentences write it (shipping_rate_card.lb_text): 0.7, 2
//	sp.zone_price_words(zone)   what a zone charges, words that start a sentence: "$35.00 at any weight", "Free at
//	                            any weight", or "4 weight tiers from 0.01 to 7 lb, $75.00 to $135.00"
//	sp.card_patterns(profile)   its product code patterns that are not blank, trimmed
//	sp.takes_every_item(profile)  whether one of its patterns is nothing but asterisks (it takes whatever is left)
//	sp.codes_sentence(profile)  "Product codes F-*-PT and F-CR-PC-*.", "Every other product.", or that it has none
//	sp.card_summary(policy, country_name)  the card in plain words, for the Review step
//	sp.uses_the_card(policy)    whether anything prices from the card: the price source, or a fallback that says
//	                            Use the rate card (shipping_policy.fallback_may_use_the_card, for an enabled zone)
//	sp.text_length(text)        the characters of a text as the server counts them (a character outside the
//	                            Basic Multilingual Plane is one, where JavaScript's length counts two)
// and CARD_LIMITS, the most a card may hold (shipping_rate_card's limits, which validate_card reports).

const CARD_LIMITS = {
	profiles: 12,
	zones: 40, // in a profile
	tiers: 60, // in a zone
	countries: 260, // in a zone
	patterns: 12, // in a profile
	pattern_length: 80,
	text: 140, // a profile's or a zone's name, a delivery method
	price: 1000000,
	weight: 10000, // pounds
	days: 60,
};

// A zone of the Review's summary names its countries when it has at most this many, and counts them otherwise.
const CARD_NAMED_COUNTRIES = 6;

// A country code as the card keeps it: two capital letters (the server's _country, which also reads " us ").
function card_country(value) {
	if (typeof value !== "string") return "";
	const code = value.trim();
	return /^[A-Za-z]{2}$/.test(code) ? code.toUpperCase() : "";
}

Object.assign(sp, {
	card_as_kept(value) {
		if (Array.isArray(value)) return value.map((entry) => sp.card_as_kept(entry));
		if (!sp.is_record(value)) return value;
		const kept = {};
		for (const key of Object.keys(value).sort()) kept[key] = sp.card_as_kept(value[key]);
		return kept;
	},

	new_card_profile(name) {
		return { match: [], name, zones: [] };
	},

	new_card_zone(name) {
		return { countries: [], days: null, method: "", name, tiers: [sp.new_card_tier(null, null, null)] };
	},

	new_card_tier(from, to, price) {
		return { from, price, to };
	},

	card_profiles(card) {
		const profiles = sp.is_record(card) ? card.profiles : null;
		return Array.isArray(profiles) ? profiles.filter(sp.is_record) : [];
	},

	card_zones(profile) {
		const zones = sp.is_record(profile) ? profile.zones : null;
		return Array.isArray(zones) ? zones.filter(sp.is_record) : [];
	},

	card_tiers(zone) {
		const tiers = sp.is_record(zone) ? zone.tiers : null;
		return Array.isArray(tiers) ? tiers.filter(sp.is_record) : [];
	},

	card_counts(card) {
		const zones = sp.card_profiles(card).flatMap((profile) => sp.card_zones(profile));
		const countries = new Set();
		for (const zone of zones) {
			for (const code of Array.isArray(zone.countries) ? zone.countries : []) {
				if (card_country(code)) countries.add(card_country(code));
			}
		}
		return {
			profiles: sp.card_profiles(card).length,
			zones: zones.length,
			tiers: zones.reduce((sum, zone) => sum + sp.card_tiers(zone).length, 0),
			countries: countries.size,
		};
	},

	counts_words(counts) {
		const { profiles, zones, tiers, countries } = counts;
		return sp.and_list([
			profiles === 1 ? __("1 profile") : __("{0} profiles", [profiles]),
			zones === 1 ? __("1 zone") : __("{0} zones", [zones]),
			tiers === 1 ? __("1 weight tier") : __("{0} weight tiers", [tiers]),
			countries === 1 ? __("1 country") : __("{0} countries", [countries]),
		]);
	},

	is_flat_zone(zone) {
		const tiers = sp.card_tiers(zone);
		return tiers.length === 1 && tiers[0].from === null && tiers[0].to === null;
	},

	pounds(value) {
		return String(Number(Number(value).toPrecision(10)));
	},

	zone_price_words(zone) {
		const tiers = sp.card_tiers(zone);
		if (!tiers.length) return __("No prices yet");
		const prices = tiers.map((tier) => tier.price).filter((price) => sp.is_number(price) && price >= 0);
		if (sp.is_flat_zone(zone)) {
			if (!prices.length) return __("One price at any weight, not typed yet");
			return prices[0] === 0 ? __("Free at any weight") : __("{0} at any weight", [sp.money(prices[0])]);
		}
		const weights = tiers.flatMap((tier) => [tier.from, tier.to]).filter((weight) => sp.is_number(weight));
		const count = tiers.length === 1 ? __("1 weight tier") : __("{0} weight tiers", [tiers.length]);
		const span = weights.length
			? __("{0} from {1} to {2} lb", [count, sp.pounds(Math.min(...weights)), sp.pounds(Math.max(...weights))])
			: count;
		if (!prices.length) return __("{0}, prices not typed yet", [span]);
		const low = Math.min(...prices);
		const high = Math.max(...prices);
		const money = (amount) => (amount === 0 ? __("free") : sp.money(amount));
		const range = low === high ? money(low) : __("{0} to {1}", [money(low), money(high)]);
		return prices.length < tiers.length ? __("{0}, {1}, a price not typed yet", [span, range]) : __("{0}, {1}", [span, range]);
	},

	card_patterns(profile) {
		return (Array.isArray(profile?.match) ? profile.match : [])
			.map((pattern) => String(pattern ?? "").trim())
			.filter(Boolean);
	},

	// Whether a profile takes every item: one of its patterns is nothing but asterisks.
	takes_every_item(profile) {
		return sp.card_patterns(profile).some((pattern) => !pattern.replace(/\*/g, ""));
	},

	// The items a profile takes, as a sentence: its product codes, every other product (a pattern of *: it takes
	// whatever the profiles above it leave), or that it has none yet.
	codes_sentence(profile) {
		const patterns = sp.card_patterns(profile);
		if (!patterns.length) return __("No product codes yet, so it takes no item.");
		if (sp.takes_every_item(profile)) return __("Every other product.");
		return __("Product codes {0}.", [sp.and_list(patterns.map((pattern) => sp.clip(pattern)))]);
	},

	// {intro, profiles: [{name, title, codes, zones: [text]}], not_delivered, none}: the card in plain words, in its
	// order. `country_name(code)` names a country. A zone with many countries is counted, not listed. Names and
	// methods are cut to 140 characters (sp.clip); `title` is a profile's whole name when it was cut, else "".
	card_summary(policy, country_name = (code) => code) {
		const card = policy?.rate_card;
		const summary = {
			intro: __(
				"The rate card, with weights in pounds and prices in US dollars. Each item takes the first profile, from the top, whose product codes fit it."
			),
			profiles: [],
			not_delivered: "",
			none: "",
		};
		if (!sp.is_record(card)) {
			summary.none = __("There is no rate card yet.");
			return summary;
		}
		const text = (value) => String(value ?? "").trim();
		summary.profiles = sp.card_profiles(card).map((profile, index) => {
			const name = text(profile.name);
			return {
				name: sp.clip(name) || __("Profile {0}, without a name", [index + 1]),
				title: sp.clip(name) === name ? "" : name,
				codes: sp.codes_sentence(profile),
				zones: sp.card_zones(profile).map((zone) => {
					const codes = (Array.isArray(zone.countries) ? zone.countries : []).map(String);
					let where = __("no country yet");
					if (codes.length > CARD_NAMED_COUNTRIES) where = __("{0} countries", [codes.length]);
					else if (codes.length) where = sp.country_list(codes.map((code) => sp.clip(country_name(code))));
					const method = sp.clip(text(zone.method));
					const prices = sp.zone_price_words(zone);
					const said = method ? __("{0}, {1}", [prices, method]) : prices;
					return __("{0}: {1}. {2}.", [sp.clip(text(zone.name)) || __("a zone without a name"), where, said]);
				}),
			};
		});
		const closed = (Array.isArray(policy?.zones) ? policy.zones : []).filter(
			(zone) => sp.is_record(zone) && zone.enabled && zone.not_shipped && Array.isArray(zone.countries)
		);
		const codes = [...new Set(closed.flatMap((zone) => zone.countries.map(String)))];
		if (codes.length) {
			summary.not_delivered = __("Delivery is not offered to {0}: their zone is not shipped to.", [
				sp.country_list(codes.map((code) => country_name(code))),
			]);
		}
		return summary;
	},

	uses_the_card(policy) {
		if (policy?.price_source === "rate_card") return true;
		const method = (fallback) => (sp.is_record(fallback) ? fallback.method : null);
		if (method(policy?.default_fallback) === "rate_card") return true;
		const zones = Array.isArray(policy?.zones) ? policy.zones : [];
		return zones.some((zone) => sp.is_record(zone) && zone.enabled && method(zone.fallback) === "rate_card");
	},

	text_length(text) {
		let count = 0;
		for (let at = 0; at < text.length; at += 1) {
			const unit = text.charCodeAt(at);
			// a high surrogate followed by a low one is one character, as the server counts it
			if (unit >= 0xd800 && unit <= 0xdbff && at + 1 < text.length) {
				const next = text.charCodeAt(at + 1);
				if (next >= 0xdc00 && next <= 0xdfff) at += 1;
			}
			count += 1;
		}
		return count;
	},
});
