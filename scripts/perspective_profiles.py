#!/usr/bin/env python3
"""
Perspective Profiles v8 -- rebuilt from scratch.

The old system naively averaged poll agree/disagree without understanding
what each question MEANS directionally. This version:

1. Classifies each question's topic AND political direction
2. Normalizes all scores so higher = more progressive, lower = more conservative
3. Builds voice stance profiles from tags
4. Matches users to voices on the normalized scale

Usage:
    python3 scripts/perspective_profiles.py
"""

import json
import os
import re
import html as html_mod
from collections import defaultdict, Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOICES_PATH = ROOT / "data" / "voices.json"
PROFILES_DIR = ROOT / "data" / "profiles"
USERS_PATH = ROOT / "data" / "mirror-users.json"

# ============================================================================
# STEP 1: QUESTION CLASSIFICATION
# ============================================================================

TOPIC_RULES = [
    ("israel-palestine", ["israel", "palestin", "gaza", "hamas", "netanyahu", "west bank", "ceasefire", "zohran mamdani"]),
    ("immigration", ["immigra", "ice ", "ice's", "ice map", "border", "deport", "migrant", "undocumented", "asylum", "refugee", "sanctuary", "birthright citizenship", "citizenship", "visa", "h-1b", "noncitizen", "afghan evacuee"]),
    ("foreign-policy", ["iran", "military", "troops", "war ", "nato", "missile", "strike", "pentagon", "defense", "army", "navy", "russia", "ukraine", "putin", "china", "venezuela", "syria", "greenland", "tariff", "trade deal", "trade ban", "trade restrict", "trade practice", "nuclear", "peace deal", "peace plan", "peace talk", "peace board", "drug cartel", "cartel", "latin america", "caribbean", "davos", "trade rule", "global order", "insurrection act", "national guard"]),
    ("climate", ["climate", "environment", "green ", "carbon", "fossil", "emissions", "wildfire", "renewable", "solar", "ev ", "clean energy"]),
    ("economy", ["econom", "tariff", "trade", "inflation", "jobs", "wage", "recession", "gdp", "tax", "budget", "deficit", "spending", "federal reserve", "interest rate", "monetary", "fed ", "gold", "silver", "investor", "stock", "market", "afford", "price", "chip", "semiconductor"]),
    ("technology", [" ai ", "ai-", "artificial intelligence", "tiktok", "social media", "algorithm", "data privacy", "surveillance", "tech compan", "nvidia", "big tech", "content moderation"]),
    ("guns", ["gun", "second amendment", "firearm", "shooting", "nra", "mass shooting"]),
    ("education", ["education", "school", "college", "university", "campus", "tuition", "student loan", "harvard", "ucla", "columbia"]),
    ("healthcare", ["healthcare", "drug pric", "medical", "vaccine", "health insurance", "medicare", "medicaid", "fda", "cdc", "measles", "mifepristone", "tylenol", "mental health", "addiction", "covid", "maha report", "rfk", "kennedy jr", "hepatitis", "flu vaccin", "mrna"]),
    ("free-speech", ["free speech", "censorship", "first amendment", "misinformation", "kimmel", "comedy", "comedian", "jokes about political", "flag", "protest art"]),
    ("civil-rights", ["civil rights", "discrimination", "dei", "diversity", "equity", "inclusion", "lgbtq", "race", "racial", "voting rights", "voter", "gerrymandering", "redistrict", "election", "mlk", "juneteenth", "marriage equality", "abortion", "roe v. wade", "reproductive"]),
    ("government-power", ["doge", "executive", "presidential power", "overreach", "congress should", "federal government", "federal agenc", "shutdown", "veto", "autopen", "federalize", "insurrection", "authoritarian", "deploy", "national guard"]),
    ("media", ["media", "journalism", "washington post", "new york times", "abc", "fcc", "cbs", "paramount", "press freedom"]),
    ("criminal-justice", ["prison", "incarcerat", "criminal justice", "police", "law enforcement", "pardon", "epstein", "indictment", "indict", "prosecution", "attorney general", "doj", "comey", "bolton", "contempt"]),
]

def classify_topic(question: str) -> str:
    q_lower = question.lower()
    for topic, keywords in TOPIC_RULES:
        for kw in keywords:
            if kw in q_lower:
                return topic
    return "other"


# Direction classification: what does "agree" mean politically?
# We classify based on the question text using pattern matching.
# "agree-progressive" = agreeing aligns with progressive positions
# "agree-conservative" = agreeing aligns with conservative positions
# "neutral" = no clear political direction

# Conservative-agreement patterns: agreeing with these = conservative position
AGREE_CONSERVATIVE_PATTERNS = [
    "tariffs are a fair",
    "should expand its travel ban",
    "expanding the travel ban",
    "military pressure if talks with iran fail",
    "should increase military pressure",
    "ice should be allowed to detain",
    "ice should get access",
    "should treat drug cartels as wartime",
    "should reduce overall immigration",
    "military should be used against foreign drug",
    "should strike iran",
    "should resume nuclear testing",
    "should deepen its partnership with hungary",
    "should control venezuela",
    "china's subsidies give local ev makers an unfair",
    "trump is keeping his promise",
    "trump has improved americans",
    "trump's speech accurately",
    "trump is right to blame the left",
    "doge was a success",
    "doge was successful",
    "maha report shows robert f. kennedy",
    "overturning roe v. wade respected",
    "right to deploy",
    "should be allowed to deploy the national guard to any",
    "should have the power to send troops into cities",
    "should have the power to change state voting rules",
    "federal government has grown too large",
    "u.s. department of education should be abolished",
    "tiktok's new u.s. ownership increases the risk",
    "president should be able to temporarily raise global tariffs",
    "national security risks outweigh the benefits of admitting",
    "public schools should display religious texts",
    "public schools should be allowed to broadcast student-led prayers",
    "burning the american flag should be a crime",
    "spending tens of billions to expand ice",
    "deploying national guard troops to assist ice is an appropriate",
    "cities should be required to cooperate with ice",
    "free speech protections should apply to ai-generated content",
    "restoring trump's social media accounts",
    "mainstream media outlets, such as the new york times, are unfair",
    "blaming joe biden for current prices is fair",
    "president trump deserves ample praise",
    "2016 russia investigation was politically motivated",
    "super bowl halftime shows have become too political",
    "the affordable care act has made health insurance too dependent",
    "president trump is right. inflation is",
    "president trump is right that shutdowns can be",
    "trump's personal relationship with putin helps",
    "president trump is accurately representing",
    "it's reasonable for companies to cut jobs if automation",
    "limiting covid shots to high-risk",
    "renaming the kennedy center after president trump",
    "most covid aid programs should be investigated",
    "deploying national guard troops to assist ice",
    "president trump was right to order strikes",
    "president trump was right to use pentagon funds",
    "trump administration was right to pause federal aid",
    "trump administration's deal with pfizer",
    "trump's expanding counterdrug operations",
    "u.s. should play a direct role in governing gaza",
    "u.s. should charge higher national park fees",
    "u.s. should commit troops to ukraine",
    "u.s. should not attempt to take control of greenland",
    "should pursue its oil interests in venezuela",
    "the u.s. should tighten export controls on advanced chips",
    "u.s. should never allow advanced ai chips",
    "replacing near-zero tariffs with a 15% rate is fair",
    "visa bonds will help enforce legal immigration",
    "zohran mamdani's democratic socialist platform would hurt",
    "i support president trump's decision to fire",
    "i support requiring top pentagon officials to sign nondisclosure",
    "i trust trump's federal agencies",
    "city leaders should welcome federal help",
    "irs should share taxpayer data with immigration",
    "i support the cdc panel's decision to stop recommending",
    "u.s. is right to use tariff threats",
    "u.s. is right to threaten 100% tariffs",
    "the house is right to hold the clintons in contempt",
    "there isn't enough scientific evidence to blame social media",
    "indictment of james comey is a legitimate effort",
    "pentagon should investigate sen. mark kelly",
    "trump administration should nationalize elections",
    "should use tariffs to pressure allied countries",
    "stricter citizenship requirements are a reasonable",
    "feds should raid all suspected sites in minnesota",
    "doj is right to subpoenaed minnesota gov",
    "should pause asylum decisions",
    "u.s. should have the right to charge venezuelan president",
    "u.s. should take tougher action against",
    "united states should maintain an ongoing military presence in syria",
    "japan should take a firmer stance against china",
    "social media monitoring of visa holders",
    "u.s. should use military threats only as a last resort",
    "both parties gerrymander",
    "trump's call to re-review afghan evacuees",
    "u.s. should keep tightening sanctions on russian oil",
    "the u.s. should prioritize building factories at home",
    "u.s. participation in global bodies like the united nations goes against",
    "federal government should be allowed to seize local election records",
    "united states should be able to pursue territorial control",
    "president should have the authority to federalize d.c.",
    "federal government should have authority over d.c.'s police",
    "u.s. government should ban tiktok",
    "bringing back the presidential fitness test",
    "western allies should conduct more joint operations",
    "teachers should be tested on political or cultural issues",
    "presidents should be allowed to replace statistical agency heads",
]

AGREE_PROGRESSIVE_PATTERNS = [
    "overreach of presidential power",
    "ice should be subject to stricter judicial oversight",
    "ice should focus primarily on individuals with serious criminal",
    "ice should not be allowed into private homes",
    "ice should not be involved in monitoring",
    "immigration enforcement has become too militarized",
    "immigration enforcement is extending too far",
    "congress should place stricter limits on federal immigration",
    "congress should withhold dhs funding",
    "stricter judicial oversight when detaining families",
    "immigration arrests should not take place inside courthouses",
    "immigration enforcement agencies should be prohibited from detaining children",
    "immigration policy should focus on expanding access to citizenship",
    "under the trump administration, ice operations",
    "illinois is right to block ice",
    "birthright citizenship should continue",
    "children born in the u.s. to undocumented immigrants should automatically",
    "census counts should include all residents",
    "federal immigration enforcement should pause",
    "tools like ice map help hold government",
    "president trump was wrong to pardon",
    "president trump has been too supportive of israel",
    "president trump is out of touch",
    "president trump's handling of the economy has made",
    "president trump's use of phrases like \"punishable by death\"",
    "president trump was wrong to call",
    "president trump was wrong to downplay",
    "president trump was wrong to try to deploy",
    "president trump went too far",
    "president trump should not be able to remove lisa cook",
    "president trump should have released the epstein files",
    "president trump should have to get congressional approval",
    "giving president trump the nobel medal undermines",
    "trump's push for greenland is driven more by personal",
    "trump is politicizing the federal reserve",
    "trump's influence over fed appointments undermines",
    "trump's attempt to fire lisa cook shows why limits",
    "pardoning changpeng zhao shows that trump is too close",
    "comey's indictment for allegedly lying to congress is a political vendetta",
    "elon musk's political donations give him too much influence",
    "it's risky for one person to control so much",
    "president trump and republicans deserve most of the blame for the current shutdown",
    "the trump administration's immigration sweeps go too far",
    "the trump administration's approach to immigration enforcement is moving the country in the wrong direction",
    "the trump justice department can no longer be trusted",
    "the no kings movement reflects genuine fears",
    "the record goods trade deficit shows president trump's tariff strategy is not working",
    "the deal shows trump prioritizes trade optics",
    "trump's proposed plan asks ukraine to make concessions",
    "trump's revised peace plan gives too much to russia",
    "putin's praise of trump shows the u.s. president is too close",
    "trump's comments could create unnecessary fear",
    "deploying the national guard inside u.s. cities crosses a line",
    "deploying national guard forces to chicago is an overreach",
    "using the national guard to address routine crime is an overreach",
    "using u.s. cities as \"training grounds\" for the military",
    "the presence of federal troops",
    "the federal government should not deploy troops to u.s. cities without state",
    "the federal government should not use infrastructure funding as leverage",
    "the president should not be able to impose broad tariffs",
    "the president should not be able to shut down a federally funded arts center",
    "the military should operate independently from any single political leader",
    "federal law enforcement should stay out of state-run election",
    "federal agencies should remain politically independent",
    "federal agencies should recognize holidays",
    "diplomacy should always come first",
    "climate",
    "the u.s. government should be doing more to support ev adoption",
    "the u.s. government should continue offering tax credits for ev",
    "the trump administration should not have canceled the $7 billion solar",
    "foreign workers are essential to meet america's clean energy",
    "marijuana should be fully legal",
    "vaccine policy decisions should be made by scientists",
    "independent advisory panels, not political figures",
    "changes to vaccine guidelines should require broad medical",
    "the cdc should always be led by a physician",
    "the cdc should do more to encourage people to get flu",
    "the u.s. should continue investing in mrna vaccine research",
    "political influence is weakening the independence of u.s. vaccine",
    "robert f. kennedy jr.'s actions as health secretary have negatively",
    "robert f. kennedy jr.'s shakeup of vaccine advisory panels is weakening",
    "the cdc's shift in wording makes me more concerned",
    "eliminating vaccine mandates in schools puts children's health",
    "political leaders like trump and rfk jr. should not make medical claims",
    "susan monarez was right to resist political pressure",
    "the government should invest more resources into fighting the measles",
    "israel's military campaign in gaza should be investigated",
    "israel's war in gaza meets the definition of genocide",
    "the high number of journalists killed in gaza suggests",
    "aid and reconstruction efforts should begin immediately",
    "countries should not be able to ban humanitarian groups",
    "france is right to recognize palestine",
    "the u.s. should allow children from gaza to receive medical",
    "media coverage has focused too heavily on trump's role",
    "protecting voting access is more important",
    "the voting rights act is still necessary",
    "the gop's redistricting push is undemocratic",
    "the trump administration's encouragement of redistricting in republican-led",
    "texas lawmakers should not be allowed to redraw",
    "states should be required to complete redistricting only once per decade",
    "marriage equality is now a settled issue",
    "a show being inclusive of lgbtq+ characters is a positive",
    "the federal government should require universal background checks",
    "tech companies should be held legally responsible for psychological harm",
    "tech companies should need permission before using people's",
    "tech companies should be more transparent",
    "if ai companies use a site's data, they should help pay",
    "governments should impose heavy penalties on tech companies",
    "it's better for the u.s. to risk slowing ai innovation with strict rules",
    "the government should step in to limit the power of dominant ai",
    "tech startups, not users, should be held responsible",
    "it is concerning for the pentagon to have full access to ai tools",
    "the u.s. should establish clear public rules before using ai",
    "ai-altered images shared by public officials should be clearly labeled",
    "ai-generated images are a threat to democracy",
    "jimmy kimmel should have the right to make jokes",
    "the kimmel suspension shows how fragile free speech",
    "fcc chairman brendan carr crossed a line",
    "it was appropriate for the fcc to pressure cbs",
    "the government should not influence which political candidates appear",
    "elected officials should never be physically targeted",
    "paparazzi should face stronger legal consequences",
    "no individual should be able to earn more than $1 trillion",
    "california should tax billionaires more",
    "the government should step in to limit corporate layoffs",
    "the government should pause aggressive debt collection",
    "the federal government should help pay for emergency care for undocumented",
    "states should temporarily fund snap benefits",
    "legal immigration should not be restricted based on potential use of public benefits",
    "international students are important to the u.s. economy",
    "harvard should be allowed to operate free from political interference",
    "detaining international students over political expression threatens",
    "columbia university should be held accountable",
    "public universities like ucla should lose federal funds if they fail to address antisemitism",
    "kamala harris has a good vision",
    "letitia james is being unfairly targeted",
    "replacing career prosecutors with personal allies weakens",
    "the effort to indict james comey looks more like political payback",
    "the indictment of john bolton appears to be politically motivated",
    "the investigation into sen. slotkin sets a dangerous precedent",
    "the judge was right to dismiss the cases against comey and james",
    "the phone call where trump asked officials to \"find\" votes",
    "lawsuits like dominion's help protect democracy",
    "the main driver of political violence in the u.s. today comes from the right",
    "ghislaine maxwell should not be granted clemency",
    "the trump administration has been fully transparent in releasing the epstein",
    "president trump has been truthful about the extent of his past relationship with jeffrey epstein",
    "the clintons are being unfairly targeted",
    "both parties should be equally investigated for their ties to epstein",
    "oregon's governor is right to call the deployment of troops",
    "san francisco's local and state officials, not washington",
    "the d.c. mayor, not the president",
    "a president should not be able to federalize a state's national guard over",
    "state officials should not mandate political groups like turning point",
    "the government should reopen before any policy negotiations",
    "centrist democrats made the right call",
    "pelosi's departure leaves a leadership vacuum",
    "senate democrats are right to block",
    "democrats are justified in redrawing california's maps",
    "democrats are right to redraw maps",
    "california's counter-map strategy is a legitimate",
    "proposition 50 was a justified response",
    "gustavo petro was right to call out the u.s.",
    "meeting with putin is a diplomatic win for russia",
    "the alaska summit was more of a win for putin",
    "it's inappropriate for dhs to use comedy clips",
    "president trump's comments about rob reiner",
    "the department of government efficiency was more about politics",
    "religious organizations should face legal consequences",
    "previous confrontations don't excuse violent or deadly actions",
    "u.s. law enforcement should never attempt to enter a foreign consulate",
    "carrying a legal firearm should not justify deadly force",
    "the ig's findings raise legitimate concerns about secretary hegseth",
    "the pentagon's new reporting rules go too far",
    "u.s. media should abide by white house requests",
    "vice president jd vance is wrong to blame liberal institutions",
    "building a ballroom at the white house is an inappropriate",
    "demolishing a historic part of the white house",
    "renaming the department of defense back to the department of war",
    "the health department should not make further cuts",
    "pope leo was right to call trump's treatment",
    "public officials should speak out when federal actions harm",
    "a prime minister should step down if credible revelations",
    "the protests against the guard presence are a legitimate response",
    "fleeing the state to block a vote is a legitimate political tactic",
    "texas democrats were right to flee the state",
    "refusing to fund the department of homeland security is an acceptable",
    "congress should not shut down a major security agency",
    "impeaching dhs secretary kristi noem is not worth",
    "de-escalating tensions in minnesota should",
    "invoking the insurrection act would escalate tensions",
    "cutting jobs and core coverage is not worth damaging trust",
    "it was right for minnesota gov. tim walz not run for reelection",
    "federal pressure on states to share voter or welfare data",
    "the decision to move u.s. space command to alabama was based more on politics",
    "zohran mamdani is a great choice",
    "as mayor, zohran mamdani will make new york city more affordable",
    "zohran mamdani's victory gives him a clear mandate",
    "when it comes to affordability, zohran mamdani has the stronger plan",
    "governments should not be able to shut off internet",
    "public spaces like the national mall are appropriate places for political protest",
    "public transit tickets in cities should be free",
    "athletes representing their country should publicly speak out",
    "the case against prince andrew demonstrates that no public figure is above scrutiny",
    "the fda made the right decision by approving",
    "spain should hold officials responsible for public transit safety",
    "spanish authorities must hold officials responsible",
    "republicans were right to break with their party over president trump's tariffs",
    "rising political tensions are making the u.s. a less reliable safe haven",
    "political instability in the u.s. is shaking investor confidence",
    "only greenland and denmark have the right to determine",
    "u.s.'s push to acquire greenland undermines international rules",
    "u.s. threats to take greenland have destabilized nato",
    "nato can no longer rely on full solidarity",
    "the u.s. should increase funding for university research",
    "barack obama's response to recent political violence was more effective",
    "do you think the federal government should fund local early-warning systems",
    "congress should make all epstein-related files public",
]

# Neutral patterns (no clear political direction)
NEUTRAL_PATTERNS = [
    "ai will replace most traditional software",
    "ai companies will enhance traditional software",
    "whoever controls inference will control",
    "turning creators into ai-powered brands",
    "showing ads is a fair trade-off",
    "apple should expand further into financial",
    "big tech should offer keep offering higher payouts",
    "studios should keep making sequels",
    "iron maiden should already be in the rock",
    "strong political network is just as valuable",
    "big companies should charge for returns",
    "all countries should end national letter delivery",
    "cities should charge tourists",
    "full-fat dairy should be part of a healthy school",
    "the ioc handled this situation appropriately",
    "sony should absorb more costs",
    "this ad campaign makes me more likely",
    "the backlash to the american eagle ad is overblown",
    "netflix should own warner bros",
    "this arrest will have lasting damage on the british monarchy",
    "there should be stronger oversight for sports betting",
    "tech giants should invest more into making chips more efficient",
    "to keep their competitive edges, big tech companies need to invest",
    "tesla should focus more on ev sales",
    "disney and abc were right to bring jimmy kimmel back",
    "disney was right to suspend kimmel",
    "consumer boycotts are an effective way",
    "as birth rates fall",
    "china's shrinking population",
    "china's reliance on exports",
    "airlines should cover travelers' hotels",
    "sports teams should be allowed to bring back names",
    "private banks should have the right to close accounts",
    "the u.s. should make improving travel freedom",
    "the u.s. should not host global sporting events",
    "the government should be involved in family planning",
    "i support setting strict national limits on hemp",
    "eyal zamir's caution about a full takeover",
    "the immediate sell-off in gold and silver",
    "reopening crossings like rafah marks a first step",
    "i support australia's new social media ban",
    "parents, not lawmakers, should take primary responsibility",
    "ai face scans are an acceptable requirement",
    "u.s. government should make improving travel freedom a foreign policy goal",
    "u.s. should offer a visa that provides a path to citizenship for people who invest",
    "the eu should broaden its trade partnerships",
    "india and europe are right to reduce their economic dependence",
    "reducing economic dependence on the u.s. should be a top priority for canada",
    "middle powers like canada should form new alliances",
    "canada was justified in airing an ad",
    "reducing russian oil imports should be a necessary step for india",
    "china's anti-corruption purge",
    "u.s. government should allow sales of older ai chips to china",
    "u.s. government should put trade restrictions on only the most advanced chips",
    "avoiding tariffs on europe was the right move",
    "security guarantees from the u.s. and europe are enough to protect ukraine",
    "ukraine is unlikely to win back all territory",
    "a peace deal that includes ukraine ceding some land is acceptable",
    "u.s. should pressure ukraine to accept a land-for-peace deal",
    "u.s. should push for a ukraine peace deal even if it involves territorial concessions",
    "the u.s. should stay out of venezuela's internal affairs",
    "backchannel talks should always remain open",
    "the incident shows a serious breakdown in coordination",
    "governments should turn to private tech companies for help",
    "the u.s. should increase diplomatic and cooperative pressure on venezuela",
    "u.s. should keep sanctions on russian oil",
    "trump deserved praise for the october 2025 hostage deal",
    "israeli was right to walk away from gaza ceasefire talks",
    "granting a pardon now would undermine faith in israel's judicial",
    "america should reduce its military footprint",
    "the u.s. should require visa-free travelers to share",
    "inflation is easing, but many prices remain too high",
    "using an autopen doesn't undermine",
    "trump should have the authority to federalize",
    "it's worth blocking the bridge",
    "the federal reserve should be completely insulated",
    "the federal reserve's decisions are becoming too politicized",
    "the federal reserve's monetary policy decisions should not be politicized",
    "should the u.s. government ban central bank digital currencies",
    "president trump's \"board of peace\" is a dangerous replacement",
    "president trump's \"america 250\" celebrations",
    "the u.s. should prioritize lowering food prices",
    "the government should pause any federal funding if fraud",
    "federal employees who are furloughed during a shutdown should always be guaranteed",
    "the u.s. should keep tightening sanctions",
    "lawsuits about tylenol and autism are driven more by fear than facts",
    "the current u.s. vaccine approval process is strict enough",
    "international peace prizes should not be used to reward",
    "academic freedom should have some limits",
    "major u.s. oil companies should invest in venezuela",
    "european leaders should align more closely with president trump's vision",
    "active-duty u.s. soldiers should not be deployed to respond to domestic protests",
    "u.s. should exempt doctors and healthcare workers from the new $100,000 h-1b",
    "tech companies have a responsibility to bring more manufacturing",
    "tech companies should have broad access to h-1b visas",
    "marijuana should remain legal but be more tightly regulated",
    "u.s. military should continue operations to stop isis",
    "u.s. military should have to obtain congressional authorization",
    "u.s. should reduce its military footprint in the middle east",
    "the u.s. should intervene in the protests in iran",
    "u.s. should not be able to revoke visas",
    "russian president vladimir putin should not be invited",
    "trump was right to use pentagon funds to ensure military members get paid",
    "democrats and republicans should align on extending healthcare subsidies",
    "the u.s. should keep tightening sanctions on russian oil",
    "congress should make all epstein-related files public",
]


def classify_direction(question: str) -> str:
    """Classify what 'agree' means for this question politically."""
    q_lower = question.lower()

    for pattern in AGREE_CONSERVATIVE_PATTERNS:
        if pattern in q_lower:
            return "agree-conservative"

    for pattern in AGREE_PROGRESSIVE_PATTERNS:
        if pattern in q_lower:
            return "agree-progressive"

    for pattern in NEUTRAL_PATTERNS:
        if pattern in q_lower:
            return "neutral"

    # Fallback heuristics
    # Questions that criticize Trump -> agree = progressive
    if any(x in q_lower for x in ["trump was wrong", "trump went too far", "trump is out of touch",
                                     "trump should not", "trump's push", "trump is politiciz",
                                     "overreach", "crosses a line", "too far", "too militarized",
                                     "threatens civil liberties", "abuse of", "undermines",
                                     "weakening", "negatively impacted"]):
        return "agree-progressive"

    # Questions that support Trump -> agree = conservative
    if any(x in q_lower for x in ["trump is right", "trump was right", "trump deserves",
                                     "trump has improved", "trump is keeping",
                                     "trump is accurately"]):
        return "agree-conservative"

    # Default to neutral if we can't determine
    return "neutral"


def normalize_response(response_value: float, direction: str) -> float:
    """
    Normalize response so higher = more progressive, lower = more conservative.
    Returns None for neutral questions (excluded from political matching).
    """
    if direction == "agree-progressive":
        return response_value  # agree = progressive, so higher = more progressive
    elif direction == "agree-conservative":
        return 1.0 - response_value  # flip: agree (1.0) -> 0.0 (conservative)
    else:
        return None  # neutral, exclude from political scoring


TOPIC_LABELS = {
    "foreign-policy": "Foreign Policy",
    "immigration": "Immigration",
    "climate": "Climate & Environment",
    "economy": "Economy & Trade",
    "technology": "Technology & AI",
    "guns": "Guns & Safety",
    "israel-palestine": "Israel-Palestine",
    "education": "Education",
    "healthcare": "Healthcare",
    "free-speech": "Free Speech & Media",
    "civil-rights": "Civil Rights & Voting",
    "government-power": "Government Power",
    "media": "Media & Press",
    "criminal-justice": "Criminal Justice",
    "culture": "Culture",
    "other": "Other",
}


# ============================================================================
# STEP 2: USER POLITICAL PROFILES
# ============================================================================

def build_question_index(responses):
    """Pre-classify all questions."""
    questions = {}
    for r in responses:
        q = r["question"]
        if q and q not in questions:
            questions[q] = {
                "topic": classify_topic(q),
                "direction": classify_direction(q),
            }
    return questions


def build_user_profile(user_responses, question_index):
    """Build a user's political profile from their poll responses."""
    all_normalized = []
    topic_normalized = defaultdict(list)
    topic_raw = defaultdict(list)
    topic_engagement = Counter()

    for r in user_responses:
        q = r["question"]
        if not q or r["response_value"] is None:
            continue

        val = 1.0 - float(r["response_value"])  # Scale is inverted in DB: 0=strongly agree, 1=strongly disagree
        qi = question_index.get(q)
        if not qi:
            continue

        topic = qi["topic"]
        direction = qi["direction"]
        topic_engagement[topic] += 1
        topic_raw[topic].append(val)

        norm = normalize_response(val, direction)
        if norm is not None:
            all_normalized.append(norm)
            topic_normalized[topic].append(norm)

    if not all_normalized:
        return None

    overall_score = sum(all_normalized) / len(all_normalized)

    # Per-topic scores (only topics with 3+ politically-classifiable responses)
    topic_scores = {}
    for topic, vals in topic_normalized.items():
        if len(vals) >= 3:
            topic_scores[topic] = sum(vals) / len(vals)

    # Signature positions: most extreme on normalized scale (closest to 0 or 1)
    response_details = []
    seen_questions = set()
    for r in user_responses:
        q = r["question"]
        if not q or r["response_value"] is None or q in seen_questions:
            continue
        seen_questions.add(q)

        val = 1.0 - float(r["response_value"])  # Scale is inverted in DB: 0=strongly agree, 1=strongly disagree
        qi = question_index.get(q)
        if not qi:
            continue

        norm = normalize_response(val, qi["direction"])
        if norm is not None:
            extremeness = abs(norm - 0.5)
            response_details.append({
                "question": q,
                "raw_value": val,
                "normalized": norm,
                "extremeness": extremeness,
                "topic": qi["topic"],
                "direction": qi["direction"],
            })

    response_details.sort(key=lambda x: x["extremeness"], reverse=True)
    signature = response_details[:3]

    return {
        "overall_score": overall_score,
        "topic_scores": topic_scores,
        "topic_engagement": dict(topic_engagement),
        "signature": signature,
        "topic_raw": dict(topic_raw),
        "all_normalized": all_normalized,
    }


# ============================================================================
# STEP 3: VOICE STANCE PROFILES
# ============================================================================

# Tag -> normalized score (0 = very conservative, 1 = very progressive)
TAG_SCORES = {
    # Progressive
    "progressive": 0.85, "progressive left": 0.90, "progressive activist": 0.90,
    "progressive policy": 0.80, "progressive commentary": 0.80,
    "progressive economics": 0.85, "progressive organizing": 0.85,
    "progressive movement": 0.85, "progressive populist": 0.80,
    "progressive foreign policy": 0.85, "progressive legal analysis": 0.85,
    "progressive caucus": 0.85,
    "liberal": 0.75, "liberalism": 0.70,
    "democratic socialist": 0.90, "Bernie-adjacent": 0.85,
    "populist left": 0.80, "populist Democrat": 0.70,
    "radical left": 0.95,

    # Democratic
    "Democratic": 0.70, "Democratic establishment": 0.65,
    "Democratic leadership": 0.70, "Democratic senator": 0.70,
    "Democratic media": 0.70, "Democratic governor": 0.70,
    "Democratic messaging": 0.65, "Democratic coalition": 0.65,
    "rising Democrat": 0.70, "swing state Democrat": 0.65,
    "centrist Democrat": 0.55, "moderate Democrat": 0.60,

    # Center-left
    "center-left": 0.65, "centrist-left": 0.60,

    # Centrist
    "centrist": 0.50, "centrist analysis": 0.50, "pragmatic centrist": 0.50,
    "bipartisan": 0.50, "nonpartisan": 0.50, "non-partisan": 0.50,
    "independent": 0.50, "balanced": 0.50, "pragmatic": 0.50,
    "heterodox": 0.50, "bridge-building": 0.50,
    "non-partisan news": 0.50, "nonpartisan research": 0.50,

    # Center-right
    "center-right": 0.35, "center-right policy": 0.35,
    "moderate conservative": 0.35,

    # Conservative
    "conservative": 0.15, "conservative policy": 0.15,
    "conservative populist": 0.15, "traditional conservative": 0.20,
    "social conservative": 0.15, "fiscal conservative": 0.25,
    "cultural conservative": 0.20, "constitutional conservative": 0.20,
    "establishment Republican": 0.25, "business Republican": 0.30,
    "traditional Republican": 0.25, "traditional": 0.25,

    # Republican
    "Republican": 0.20, "Republican establishment": 0.25,
    "Republican leadership": 0.20, "Republican senator": 0.20,
    "Republican strategy": 0.20, "rising Republican": 0.20,
    "maverick Republican": 0.30, "pragmatic Republican": 0.30,
    "House Republican leadership": 0.15,

    # MAGA / far-right
    "MAGA": 0.10, "MAGA conservative": 0.10, "MAGA populist": 0.10,
    "MAGA firebrand": 0.08, "pro-Trump": 0.10, "Trump ally": 0.10,
    "Trump campaign": 0.10, "far-right": 0.05, "Trump beat": 0.45,

    # Populist right
    "populist right": 0.20, "populist conservative": 0.20,
    "new right": 0.15, "economic nationalism": 0.25,

    # Libertarian
    "libertarian": 0.30, "libertarian-leaning": 0.35,
    "libertarian Republican": 0.25, "free markets": 0.30,
    "free-market policy": 0.30, "limited government": 0.30,
    "fiscal conservatism": 0.25, "fiscal restraint": 0.30,
    "fiscal independence": 0.30,

    # Anti-establishment (depends on context, default center)
    "anti-establishment": 0.45, "populist": 0.45,
    "contrarian": 0.45, "anti-corporate": 0.65,
    "anti-woke": 0.25, "anti-PC": 0.25, "culture warrior": 0.15,
    "anti-DEI": 0.20, "anti-BLM": 0.15,
    "anti-Trump": 0.60, "anti-Trump Republican": 0.55, "never-Trump": 0.55,
    "anti-war": 0.65, "anti-interventionist": 0.65, "non-interventionist": 0.65,
    "anti-imperialist": 0.80, "anti-billionaire": 0.85,
    "anti-monopoly": 0.70, "anti-censorship": 0.45, "anti-surveillance": 0.50,
    "anti-media": 0.30,

    # Issue-specific
    "pro-Israel": 0.25, "Palestine": 0.80, "Palestine solidarity": 0.85,
    "pro-immigration": 0.80, "immigrant rights": 0.80, "immigration advocate": 0.80,
    "immigration reform": 0.70, "immigration": 0.60,
    "anti-immigration": 0.10, "immigration enforcement": 0.15,
    "immigration hardliner": 0.10, "border hawk": 0.10,
    "border enforcement": 0.15, "border security": 0.15,
    "gun reform": 0.80, "gun safety": 0.80, "Second Amendment": 0.15,
    "pro-labor": 0.80, "pro-worker": 0.80, "worker power": 0.85,
    "labor rights": 0.80, "labor movement": 0.80, "unions": 0.80,
    "racial justice": 0.85, "racial equity": 0.85, "civil rights": 0.80,
    "anti-racism education": 0.85, "social justice": 0.85,
    "LGBTQ+": 0.85, "LGBTQ+ rights": 0.85, "trans perspective": 0.90,
    "reproductive rights": 0.85, "women's rights": 0.80,
    "voting rights": 0.80, "voter mobilization": 0.75,
    "criminal justice reform": 0.75, "police accountability": 0.80,
    "climate activism": 0.85, "climate advocacy": 0.85,
    "climate accountability": 0.80, "climate science": 0.70,
    "environmental justice": 0.85, "environmental policy": 0.75,
    "sustainability science": 0.70, "climate economics": 0.65,
    "greenwash busting": 0.75, "sustainable living": 0.70,
    "AI ethics": 0.65, "AI regulation": 0.60, "AI risk": 0.55,
    "tech regulation": 0.60, "tech accountability": 0.65,
    "tech critic": 0.60, "humane tech": 0.65, "tech-conservative": 0.30,
    "defense hawk": 0.20, "foreign policy hawk": 0.20, "hawkish": 0.20,
    "national security": 0.40, "defense": 0.35, "defense policy": 0.35,
    "China hawk": 0.25, "China competition": 0.35,
    "foreign policy": 0.45, "foreign policy restraint": 0.65,
    "foreign policy critic": 0.70,
    "free speech": 0.45, "press freedom": 0.60,
    "media critic": 0.45, "media criticism": 0.40, "media skeptic": 0.35,
    "health freedom": 0.35, "conspiracy-leaning": 0.35,
    "culturally progressive": 0.70,
    "economic inequality": 0.80, "economic justice": 0.80,
    "consumer protection": 0.70, "corporate accountability": 0.70,
    "dark money": 0.70, "money in politics": 0.70, "corporate power": 0.70,
    "school choice": 0.25, "education equity": 0.80,
    "Supreme Court reform": 0.75, "constitutional law": 0.50,
    "rule of law": 0.60, "oversight": 0.55,
    "democracy": 0.65, "democratic access": 0.75, "democratic institutions": 0.60,
    "Trump accountability": 0.70,
    "Project 2025": 0.10, "government reform": 0.30, "government restructuring": 0.25,
    "government oversight": 0.45,
    "1619 Project": 0.85,
    "opportunity conservatism": 0.30, "faith-based": 0.30,
    "evangelical": 0.20, "Catholic": 0.30, "traditional values": 0.20,
    "constitutionalist": 0.25, "constitutional originalist": 0.20,
    "states rights": 0.25,
    "Turning Point USA": 0.10, "Freedom Caucus": 0.10,
    "Fox News": 0.25, "MSNBC": 0.75, "CNN": 0.50,
    "The Dispatch": 0.35,
    "personal responsibility": 0.30, "self-improvement": 0.40,
    "Gen Z conservative": 0.20, "youth right-wing": 0.20,
    "manosphere": 0.20, "bro-culture": 0.35,
    "Black conservative": 0.25, "black conservative": 0.25,
    "Latina conservative": 0.25, "immigrant conservative": 0.25, "conservative immigrant": 0.25,
    "black perspective": 0.60, "Black media": 0.65,
    "Latino media": 0.65, "Latino politics": 0.65, "LatinX identity": 0.70,
    "Latina journalism": 0.65, "Latino culture": 0.65, "Latino electorate": 0.60,
    "Asian-American identity": 0.60, "Asian-American politics": 0.65,
    "Muslim perspective": 0.65, "Iranian diaspora": 0.55,
    "refugee experience": 0.75, "immigrant perspective": 0.55,
    "immigrant success": 0.40, "legal immigration": 0.40,
    "ex-progressive": 0.40, "party switcher": 0.40,
    "provocateur": 0.20,
}

# Topic-specific tag overrides
TAG_TOPIC_SCORES = {
    # Foreign policy
    "anti-war": {"foreign-policy": 0.80},
    "anti-interventionist": {"foreign-policy": 0.80},
    "non-interventionist": {"foreign-policy": 0.80},
    "foreign policy restraint": {"foreign-policy": 0.75},
    "foreign policy critic": {"foreign-policy": 0.75},
    "anti-imperialist": {"foreign-policy": 0.85},
    "defense hawk": {"foreign-policy": 0.15},
    "foreign policy hawk": {"foreign-policy": 0.15},
    "hawkish": {"foreign-policy": 0.15},
    "national security": {"foreign-policy": 0.35},
    "China hawk": {"foreign-policy": 0.20},
    "progressive foreign policy": {"foreign-policy": 0.85},
    "Russia expert": {"foreign-policy": 0.50},
    "Ukraine conflict": {"foreign-policy": 0.50},
    "internationalist": {"foreign-policy": 0.70},

    # Immigration
    "anti-immigration": {"immigration": 0.10},
    "immigration enforcement": {"immigration": 0.15},
    "immigration hardliner": {"immigration": 0.10},
    "border hawk": {"immigration": 0.10},
    "border enforcement": {"immigration": 0.15},
    "border security": {"immigration": 0.15},
    "pro-immigration": {"immigration": 0.85},
    "immigrant rights": {"immigration": 0.85},
    "immigration advocate": {"immigration": 0.85},
    "immigration reform": {"immigration": 0.75},
    "immigration": {"immigration": 0.65},
    "immigrant conservative": {"immigration": 0.40},
    "conservative immigrant": {"immigration": 0.40},

    # Climate
    "climate activism": {"climate": 0.90},
    "climate advocacy": {"climate": 0.85},
    "climate accountability": {"climate": 0.80},
    "climate science": {"climate": 0.75},
    "environmental justice": {"climate": 0.85},
    "environmental policy": {"climate": 0.75},
    "anti-doomism": {"climate": 0.60},
    "sustainability science": {"climate": 0.75},
    "climate economics": {"climate": 0.70},
    "greenwash busting": {"climate": 0.80},

    # Guns
    "gun reform": {"guns": 0.85},
    "gun safety": {"guns": 0.85},
    "Second Amendment": {"guns": 0.15},

    # Israel-Palestine
    "pro-Israel": {"israel-palestine": 0.20},
    "Palestine": {"israel-palestine": 0.80},
    "Palestine solidarity": {"israel-palestine": 0.85},

    # Civil rights
    "racial justice": {"civil-rights": 0.85},
    "racial equity": {"civil-rights": 0.85},
    "civil rights": {"civil-rights": 0.80},
    "social justice": {"civil-rights": 0.85},
    "LGBTQ+": {"civil-rights": 0.85},
    "LGBTQ+ rights": {"civil-rights": 0.85},
    "voting rights": {"civil-rights": 0.80},
    "anti-DEI": {"civil-rights": 0.15},
    "anti-woke": {"civil-rights": 0.20},
    "anti-BLM": {"civil-rights": 0.15},
    "culture warrior": {"civil-rights": 0.15},

    # Economy
    "economic nationalism": {"economy": 0.25},
    "fiscal conservatism": {"economy": 0.20},
    "free markets": {"economy": 0.25},
    "economic justice": {"economy": 0.80},
    "economic inequality": {"economy": 0.80},
    "pro-labor": {"economy": 0.80},
    "pro-worker": {"economy": 0.80},
    "progressive economics": {"economy": 0.80},
    "worker power": {"economy": 0.85},
    "labor rights": {"economy": 0.80},
    "anti-monopoly": {"economy": 0.70},
    "anti-billionaire": {"economy": 0.85},

    # Healthcare
    "health freedom": {"healthcare": 0.25},
    "healthcare policy": {"healthcare": 0.55},
    "drug pricing": {"healthcare": 0.70},
    "public health": {"healthcare": 0.65},
    "Medicare for All": {"healthcare": 0.90},

    # Technology
    "AI ethics": {"technology": 0.70},
    "AI regulation": {"technology": 0.65},
    "AI risk": {"technology": 0.60},
    "tech regulation": {"technology": 0.65},
    "tech accountability": {"technology": 0.70},
    "humane tech": {"technology": 0.70},
    "tech-conservative": {"technology": 0.30},
    "tech policy": {"technology": 0.60},

    # Free speech
    "free speech": {"free-speech": 0.40},
    "anti-censorship": {"free-speech": 0.40},
    "press freedom": {"free-speech": 0.65},
    "content moderation": {"free-speech": 0.65},
}


def build_voice_profile(voice):
    """Build a voice's political profile from their tags."""
    tags = voice.get("tags", [])
    if not tags:
        return {"overall": 0.5, "topics": {}, "classified": False}

    # Overall score from tags
    scores = []
    for tag in tags:
        # Try exact match first, then lowercase
        if tag in TAG_SCORES:
            scores.append(TAG_SCORES[tag])
        elif tag.lower() in TAG_SCORES:
            scores.append(TAG_SCORES[tag.lower()])

    overall = sum(scores) / len(scores) if scores else 0.5
    classified = len(scores) > 0

    # Topic-specific scores
    topic_scores = defaultdict(list)
    for tag in tags:
        tag_key = tag if tag in TAG_TOPIC_SCORES else (tag.lower() if tag.lower() in TAG_TOPIC_SCORES else None)
        if tag_key and tag_key in TAG_TOPIC_SCORES:
            for topic, score in TAG_TOPIC_SCORES[tag_key].items():
                topic_scores[topic].append(score)

    # For topics without specific tags, use dampened overall
    topic_avgs = {}
    for topic in TOPIC_LABELS:
        if topic in topic_scores:
            topic_avgs[topic] = sum(topic_scores[topic]) / len(topic_scores[topic])
        else:
            # Dampen overall lean for unspecified topics
            topic_avgs[topic] = overall * 0.7 + 0.5 * 0.3  # Blend toward center

    return {"overall": overall, "topics": topic_avgs, "classified": classified}


# ============================================================================
# STEP 4: MATCHING
# ============================================================================

def match_user_to_voices(user_profile, voice_profiles):
    """Match a user to voices based on normalized scores."""
    distances = []

    for voice_id, vp in voice_profiles.items():
        voice = vp["voice"]
        voice_score = vp["profile"]["overall"]

        # Overall distance
        overall_dist = abs(user_profile["overall_score"] - voice_score)

        # Topic-level matching where both have data
        topic_dists = {}
        topic_matches = 0
        weighted_topic_dist = 0
        for topic, user_score in user_profile["topic_scores"].items():
            if topic in vp["profile"]["topics"]:
                voice_topic = vp["profile"]["topics"][topic]
                dist = abs(user_score - voice_topic)
                topic_dists[topic] = {
                    "user": user_score,
                    "voice": voice_topic,
                    "distance": dist,
                }
                weighted_topic_dist += dist
                topic_matches += 1

        # Combined score: 60% overall + 40% topic average
        if topic_matches > 0:
            avg_topic_dist = weighted_topic_dist / topic_matches
            combined_dist = 0.6 * overall_dist + 0.4 * avg_topic_dist
        else:
            combined_dist = overall_dist

        similarity = max(0, 1.0 - combined_dist)

        # Find alignment/divergence topics
        sorted_topics = sorted(topic_dists.items(), key=lambda x: x[1]["distance"])
        align_topics = [t for t, _ in sorted_topics[:3]]
        differ_topics = [t for t, _ in sorted_topics[-3:]] if len(sorted_topics) >= 3 else [t for t, _ in sorted_topics]

        # Build explanation
        if align_topics:
            align_explain = build_match_explanation(user_profile, topic_dists, align_topics, "align")
        else:
            align_explain = ""

        if differ_topics:
            differ_explain = build_match_explanation(user_profile, topic_dists, differ_topics, "differ")
        else:
            differ_explain = ""

        distances.append({
            "voice": voice,
            "similarity": similarity,
            "combined_dist": combined_dist,
            "align_topics": align_topics,
            "differ_topics": differ_topics,
            "align_explain": align_explain,
            "differ_explain": differ_explain,
        })

    distances.sort(key=lambda x: x["similarity"], reverse=True)

    # Ensure diversity: don't return 5 voices from same category
    closest = []
    seen_categories = Counter()
    for d in distances:
        cat = d["voice"].get("category", "")
        if seen_categories[cat] < 3:  # Max 3 from same category
            closest.append(d)
            seen_categories[cat] += 1
        if len(closest) >= 5:
            break

    # Most different
    different = sorted(distances, key=lambda x: x["similarity"])[:3]

    return closest, different


def build_match_explanation(user_profile, topic_dists, topics, mode):
    """Build human-readable explanation for why voices match/differ."""
    parts = []
    for topic in topics[:2]:
        if topic not in topic_dists:
            continue
        td = topic_dists[topic]
        user_score = td["user"]
        label = TOPIC_LABELS.get(topic, topic)

        if mode == "align":
            if user_score > 0.6:
                parts.append(f"progressive on {label}")
            elif user_score < 0.4:
                parts.append(f"conservative on {label}")
            else:
                parts.append(f"moderate on {label}")
        else:
            parts.append(label)

    if mode == "align" and parts:
        return "Both " + " and ".join(parts)
    elif mode == "differ" and parts:
        return "Diverge on " + " and ".join(parts)
    return ""


# ============================================================================
# STEP 5: COMPUTE QUESTION AVERAGES FROM DATA
# ============================================================================

def compute_question_averages(all_responses):
    """Compute average response per question from the data itself."""
    q_vals = defaultdict(list)
    for r in all_responses:
        if r["question"] and r["response_value"] is not None:
            q_vals[r["question"]].append(float(r["response_value"]))

    return {q: {"avg": sum(vals)/len(vals), "count": len(vals)} for q, vals in q_vals.items()}


# ============================================================================
# HTML GENERATION
# ============================================================================

def response_label(val):
    if val <= 0.125: return "Strongly Disagree"
    elif val <= 0.375: return "Disagree"
    elif val <= 0.625: return "Neutral"
    elif val <= 0.875: return "Agree"
    else: return "Strongly Agree"


def stance_label(normalized_score):
    """Describe a normalized score (0=conservative, 1=progressive) in context."""
    if normalized_score >= 0.75: return "strongly progressive"
    elif normalized_score >= 0.6: return "lean progressive"
    elif normalized_score >= 0.45: return "moderate/center"
    elif normalized_score >= 0.3: return "lean conservative"
    else: return "strongly conservative"


def stance_color(normalized_score):
    """Color for normalized score. Blue=progressive, coral=conservative."""
    if normalized_score >= 0.65: return "#6C9BF2"
    elif normalized_score >= 0.45: return "#888"
    else: return "#FF6343"


def interpret_compass(score):
    """Text interpretation of overall normalized score."""
    if score >= 0.75:
        return "You lean significantly progressive. You consistently favor government intervention, social equity, and institutional reform."
    elif score >= 0.60:
        return "You lean progressive. You generally support social programs, climate action, and civil rights expansion, while questioning concentrated power."
    elif score >= 0.53:
        return "You lean slightly progressive. You often side with progressive policies but show moderate instincts on select issues."
    elif score >= 0.47:
        return "You sit near the center. You draw from both progressive and conservative ideas, depending on the issue."
    elif score >= 0.40:
        return "You lean slightly conservative. You tend toward fiscal restraint and institutional stability, while staying open on social issues."
    elif score >= 0.30:
        return "You lean conservative. You generally favor free markets, traditional values, and a strong national defense."
    else:
        return "You lean significantly conservative. You consistently prioritize limited government, individual liberty, and traditional institutions."


def generate_profile_html(user, user_profile, closest, different, question_avgs, is_jack=False):
    """Generate email-safe HTML profile (tables, inline styles)."""
    first_name = html_mod.escape((user["first_name"] or "").strip().title())
    polls_count = user["polls_answered_count"]
    score = user_profile["overall_score"]
    compass_text = interpret_compass(score)

    # Compass bar: blue (left/progressive) to coral (right/conservative)
    # Score 1.0 = progressive = far left of bar
    # Score 0.0 = conservative = far right of bar
    # So dot position = (1 - score) * 100 to put progressive on left
    dot_pct = (1.0 - score) * 100
    dot_pct = max(4, min(96, dot_pct))

    # Jack's intro block
    jack_intro = ""
    if is_jack:
        jack_intro = '''
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:32px;">
      <tr><td style="padding:24px; background:#141416; border-radius:16px; border:1px solid #1F1F1F;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#FF6343; text-transform:uppercase; letter-spacing:1.5px; font-weight:600; padding-bottom:12px;">A note from Jack, founder of Newsreel</td></tr>
          <tr><td style="font-family:'DM Sans',Helvetica,Arial,sans-serif; font-size:14px; color:#ccc; line-height:1.7;">
            You're one of our top 50 most active users &mdash; out of thousands of people who use Newsreel, you've engaged with more polls than almost anyone. That's honestly amazing.<br><br>
            So we built something new for you. We track 257 public voices across politics, media, and culture &mdash; politicians, journalists, creators, commentators. We mapped your poll answers against all of them to see whose worldview most closely matches yours (and who challenges you the most).<br><br>
            This is a first look. I'd genuinely love to know what you think &mdash; does it feel accurate? Surprising? Completely wrong? Just reply to this email. Your feedback shapes what we build next.<br><br>
            - Jack
          </td></tr>
        </table>
      </td></tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td style="border-top:1px solid #1F1F1F; padding:0; height:1px; line-height:1px;">&nbsp;</td></tr></table>
    <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td style="height:32px;">&nbsp;</td></tr></table>
'''

    # Topic bars
    topic_bars_html = ""
    sorted_topics = sorted(
        [(t, s) for t, s in user_profile["topic_scores"].items()],
        key=lambda x: user_profile["topic_engagement"].get(x[0], 0),
        reverse=True,
    )

    for topic, normalized_avg in sorted_topics:
        label = TOPIC_LABELS.get(topic, topic)
        count = user_profile["topic_engagement"].get(topic, 0)
        stance = stance_label(normalized_avg)
        color = stance_color(normalized_avg)

        # Bar: 0% = conservative (right), 100% = progressive (left)
        bar_pct = max(5, min(95, normalized_avg * 100))

        topic_bars_html += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:16px;">
          <tr>
            <td style="font-family:'DM Sans',Helvetica,Arial,sans-serif; font-size:13px; color:#FFFFFF; font-weight:500;">{label}</td>
            <td align="right" style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#666;">{count} polls &middot; {stance}</td>
          </tr>
          <tr><td colspan="2" style="padding-top:6px;">
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td style="height:6px; background:#1F1F1F; border-radius:3px;">
                  <table width="{bar_pct:.0f}%" cellpadding="0" cellspacing="0" border="0">
                    <tr><td style="height:6px; background:{color}; border-radius:3px;"></td></tr>
                  </table>
                </td>
              </tr>
            </table>
          </td></tr>
        </table>'''

    # Closest voices
    closest_html = ""
    for match in closest:
        v = match["voice"]
        sim = match["similarity"]
        pct = int(sim * 100)
        name = html_mod.escape(v.get("name", v.get("id", "")))
        category = html_mod.escape(v.get("category", "").title())
        approach = html_mod.escape(v.get("approach", ""))
        photo_url = v.get("photo", "")
        if photo_url.startswith("/photos/"):
            photo_url = f"https://newsreel-perspectives.onrender.com{photo_url}"
        explain = html_mod.escape(match.get("align_explain", ""))
        if not explain:
            align_topics = ", ".join(TOPIC_LABELS.get(t, t) for t in match["align_topics"][:2])
            explain = f"Closest on {align_topics}" if align_topics else ""

        closest_html += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:8px;">
          <tr><td style="padding:16px; background:#141416; border-radius:12px; border:1px solid #1F1F1F;">
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td width="62" valign="top" style="padding-right:14px;">
                  <img src="{photo_url}" width="48" height="48" style="width:48px; height:48px; border-radius:50%; display:block; background:#1F1F1F;" alt="{name}">
                </td>
                <td valign="top">
                  <table width="100%" cellpadding="0" cellspacing="0" border="0">
                    <tr>
                      <td style="font-family:'DM Sans',Helvetica,Arial,sans-serif; font-size:15px; color:#FFFFFF; font-weight:600;">{name}</td>
                      <td align="right" style="font-family:'IBM Plex Mono',monospace; font-size:12px; color:#FF6343; font-weight:600;">{pct}%</td>
                    </tr>
                    <tr><td colspan="2" style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#666; text-transform:uppercase; letter-spacing:0.8px; padding:2px 0 4px;">{category} &middot; {approach}</td></tr>
                    <tr><td colspan="2" style="font-family:'DM Sans',Helvetica,Arial,sans-serif; font-size:12px; color:#888; line-height:1.4;">{explain}</td></tr>
                  </table>
                </td>
              </tr>
            </table>
          </td></tr>
        </table>'''

    # Different voices
    different_html = ""
    for match in different:
        v = match["voice"]
        sim = match["similarity"]
        pct = int(sim * 100)
        name = html_mod.escape(v.get("name", v.get("id", "")))
        category = html_mod.escape(v.get("category", "").title())
        approach = html_mod.escape(v.get("approach", ""))
        photo_url = v.get("photo", "")
        if photo_url.startswith("/photos/"):
            photo_url = f"https://newsreel-perspectives.onrender.com{photo_url}"
        explain = html_mod.escape(match.get("differ_explain", ""))
        if not explain:
            differ_topics = ", ".join(TOPIC_LABELS.get(t, t) for t in match["differ_topics"][:2])
            explain = f"Diverge on {differ_topics}" if differ_topics else ""

        different_html += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:8px;">
          <tr><td style="padding:16px; background:#141416; border-radius:12px; border:1px solid #1F1F1F;">
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td width="62" valign="top" style="padding-right:14px;">
                  <img src="{photo_url}" width="48" height="48" style="width:48px; height:48px; border-radius:50%; display:block; background:#1F1F1F;" alt="{name}">
                </td>
                <td valign="top">
                  <table width="100%" cellpadding="0" cellspacing="0" border="0">
                    <tr>
                      <td style="font-family:'DM Sans',Helvetica,Arial,sans-serif; font-size:15px; color:#FFFFFF; font-weight:600;">{name}</td>
                      <td align="right" style="font-family:'IBM Plex Mono',monospace; font-size:12px; color:#666; font-weight:600;">{pct}%</td>
                    </tr>
                    <tr><td colspan="2" style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#666; text-transform:uppercase; letter-spacing:0.8px; padding:2px 0 4px;">{category} &middot; {approach}</td></tr>
                    <tr><td colspan="2" style="font-family:'DM Sans',Helvetica,Arial,sans-serif; font-size:12px; color:#888; line-height:1.4;">{explain}</td></tr>
                  </table>
                </td>
              </tr>
            </table>
          </td></tr>
        </table>'''

    # Signature positions
    signature_html = ""
    for sig in user_profile["signature"]:
        q = html_mod.escape(sig["question"])
        raw_val = sig["raw_value"]
        norm_val = sig["normalized"]
        direction = sig["direction"]
        label = response_label(raw_val)

        # Crowd average
        avg_data = question_avgs.get(sig["question"], {"avg": 0.5, "count": 0})
        avg_val = float(avg_data["avg"])
        avg_label = response_label(avg_val)
        count = int(avg_data["count"])

        # Color based on extremeness of normalized position
        if norm_val >= 0.7:
            color = "#6C9BF2"  # Progressive
        elif norm_val <= 0.3:
            color = "#FF6343"  # Conservative
        else:
            color = "#888"

        user_pct = max(2, min(98, raw_val * 100))
        avg_pct = max(2, min(98, avg_val * 100))

        signature_html += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:12px;">
          <tr><td style="padding:20px; background:#141416; border-radius:12px; border:1px solid #1F1F1F;">
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr><td style="font-family:'DM Sans',Helvetica,Arial,sans-serif; font-size:14px; color:#FFFFFF; line-height:1.4; padding-bottom:12px;">"{q}"</td></tr>
              <tr><td>
                <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:8px;">
                  <tr><td style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:{color}; text-transform:uppercase; letter-spacing:0.5px; padding-bottom:4px;">You: {label}</td></tr>
                  <tr><td style="height:4px; background:#1F1F1F; border-radius:2px;">
                    <table width="{user_pct:.0f}%" cellpadding="0" cellspacing="0" border="0"><tr><td style="height:4px; background:{color}; border-radius:2px;"></td></tr></table>
                  </td></tr>
                </table>
              </td></tr>
              <tr><td>
                <table width="100%" cellpadding="0" cellspacing="0" border="0">
                  <tr><td style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#555; text-transform:uppercase; letter-spacing:0.5px; padding-bottom:4px;">Everyone ({count} votes): {avg_label}</td></tr>
                  <tr><td style="height:4px; background:#1F1F1F; border-radius:2px;">
                    <table width="{avg_pct:.0f}%" cellpadding="0" cellspacing="0" border="0"><tr><td style="height:4px; background:#444; border-radius:2px;"></td></tr></table>
                  </td></tr>
                </table>
              </td></tr>
              <tr><td style="padding-top:8px;">
                <table width="100%" cellpadding="0" cellspacing="0" border="0">
                  <tr>
                    <td style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#444;">Strongly Disagree</td>
                    <td align="center" style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#444;">Neutral</td>
                    <td align="right" style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#444;">Strongly Agree</td>
                  </tr>
                </table>
              </td></tr>
            </table>
          </td></tr>
        </table>'''

    # Full HTML (email-safe: tables, inline styles, no flexbox, no position:absolute)
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your Perspective Profile - Newsreel</title>
</head>
<body style="margin:0; padding:0; background:#0a0a0b; color:#fff; font-family:'DM Sans',Helvetica,Arial,sans-serif; -webkit-font-smoothing:antialiased;">

<table width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#0a0a0b">
  <tr><td align="center">
    <table width="560" cellpadding="0" cellspacing="0" border="0" style="max-width:560px; width:100%; padding:0 20px;">

      <!-- Accent line -->
      <tr><td style="height:3px; background:#FF6343;"></td></tr>

      <!-- Header -->
      <tr><td style="padding:24px 0 16px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>
            <td style="font-family:'Bree Serif',Georgia,serif; font-size:18px; color:#FFFFFF; letter-spacing:-0.3px;">newsreel</td>
            <td align="right" style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#555; text-transform:uppercase; letter-spacing:1px;">The Mirror</td>
          </tr>
        </table>
      </td></tr>

      <tr><td style="border-top:1px solid #1F1F1F; padding:0; height:1px; line-height:1px;">&nbsp;</td></tr>
      <tr><td style="height:32px;">&nbsp;</td></tr>

      <!-- Greeting -->
      <tr><td style="padding-bottom:8px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="font-family:'DM Sans',sans-serif; font-size:16px; color:#fff; line-height:1.6; padding-bottom:8px;">
            Hey {first_name},
          </td></tr>
          <tr><td style="font-family:'DM Sans',sans-serif; font-size:15px; color:#999; line-height:1.6;">
            You've answered <span style="color:#FF6343; font-weight:600;">{polls_count} polls</span> on Newsreel. Here's what we learned about how you see the world.
          </td></tr>
        </table>
      </td></tr>

      <tr><td style="height:32px;">&nbsp;</td></tr>
      <tr><td style="border-top:1px solid #1F1F1F; padding:0; height:1px; line-height:1px;">&nbsp;</td></tr>
      <tr><td style="height:32px;">&nbsp;</td></tr>

      <!-- Jack intro (only for Jack's email) -->
      <tr><td>{jack_intro}</td></tr>

      <!-- Compass -->
      <tr><td style="padding-bottom:32px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#FF6343; text-transform:uppercase; letter-spacing:1.5px; font-weight:600; padding-bottom:16px;">Your Compass</td></tr>
          <tr><td style="padding:24px; background:#141416; border-radius:16px; border:1px solid #1F1F1F;">

            <!-- Compass bar with dot -->
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:16px;">
              <tr>
                <td width="{dot_pct:.0f}%" valign="middle" style="font-size:0; line-height:0;">
                  <div style="height:8px; background:#6C9BF2; border-radius:4px 0 0 4px; opacity:0.4;">&nbsp;</div>
                </td>
                <td width="20" valign="middle" align="center" style="font-size:0; line-height:0;">
                  <div style="width:18px; height:18px; border-radius:50%; background:#FF6343; border:2.5px solid #1F2937;">&nbsp;</div>
                </td>
                <td valign="middle" style="font-size:0; line-height:0;">
                  <div style="height:8px; background:#FF6343; border-radius:0 4px 4px 0; opacity:0.4;">&nbsp;</div>
                </td>
              </tr>
            </table>

            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:16px;">
              <tr>
                <td style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#6C9BF2; text-transform:uppercase; letter-spacing:0.5px;">Progressive</td>
                <td align="center" style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#888; text-transform:uppercase; letter-spacing:0.5px;">Center</td>
                <td align="right" style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#FF6343; text-transform:uppercase; letter-spacing:0.5px;">Conservative</td>
              </tr>
            </table>

            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr><td style="font-family:'DM Sans',sans-serif; font-size:13px; color:#999; line-height:1.5;">{compass_text}</td></tr>
            </table>

          </td></tr>
        </table>
      </td></tr>

      <tr><td style="border-top:1px solid #1F1F1F; padding:0; height:1px; line-height:1px;">&nbsp;</td></tr>
      <tr><td style="height:32px;">&nbsp;</td></tr>

      <!-- Voices aligned -->
      <tr><td style="padding-bottom:32px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#FF6343; text-transform:uppercase; letter-spacing:1.5px; font-weight:600; padding-bottom:4px;">Voices You Align With</td></tr>
          <tr><td style="font-family:'DM Sans',sans-serif; font-size:13px; color:#666; padding-bottom:16px;">
            Out of 257 tracked voices across politics, media, and culture.
          </td></tr>
          <tr><td>{closest_html}</td></tr>
        </table>
      </td></tr>

      <tr><td style="border-top:1px solid #1F1F1F; padding:0; height:1px; line-height:1px;">&nbsp;</td></tr>
      <tr><td style="height:32px;">&nbsp;</td></tr>

      <!-- Voices that challenge -->
      <tr><td style="padding-bottom:32px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#FF6343; text-transform:uppercase; letter-spacing:1.5px; font-weight:600; padding-bottom:4px;">Voices That Challenge You</td></tr>
          <tr><td style="font-family:'DM Sans',sans-serif; font-size:13px; color:#666; padding-bottom:16px;">
            The perspectives furthest from your positions.
          </td></tr>
          <tr><td>{different_html}</td></tr>
        </table>
      </td></tr>

      <tr><td style="border-top:1px solid #1F1F1F; padding:0; height:1px; line-height:1px;">&nbsp;</td></tr>
      <tr><td style="height:32px;">&nbsp;</td></tr>

      <!-- Signature positions -->
      <tr><td style="padding-bottom:32px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#FF6343; text-transform:uppercase; letter-spacing:1.5px; font-weight:600; padding-bottom:4px;">Your Signature Positions</td></tr>
          <tr><td style="font-family:'DM Sans',sans-serif; font-size:13px; color:#666; padding-bottom:16px;">
            The polls where you had your strongest opinions, compared to how everyone else answered.
          </td></tr>
          <tr><td>{signature_html}</td></tr>
        </table>
      </td></tr>

      <tr><td style="border-top:1px solid #1F1F1F; padding:0; height:1px; line-height:1px;">&nbsp;</td></tr>
      <tr><td style="height:32px;">&nbsp;</td></tr>

      <!-- Topic map -->
      <tr><td style="padding-bottom:32px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#FF6343; text-transform:uppercase; letter-spacing:1.5px; font-weight:600; padding-bottom:4px;">Your Topic Map</td></tr>
          <tr><td style="font-family:'DM Sans',sans-serif; font-size:13px; color:#666; padding-bottom:16px;">
            Your average stance across topics where you've answered 3+ polls. Bars show progressive (blue) to conservative (coral).
          </td></tr>
          <tr><td style="padding:20px; background:#141416; border-radius:16px; border:1px solid #1F1F1F;">
            {topic_bars_html}
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="padding-top:8px; border-top:1px solid #1F1F1F;">
              <tr>
                <td style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#6C9BF2;">Progressive</td>
                <td align="center" style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#888;">Center</td>
                <td align="right" style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#FF6343;">Conservative</td>
              </tr>
            </table>
          </td></tr>
        </table>
      </td></tr>

      <tr><td style="border-top:1px solid #1F1F1F; padding:0; height:1px; line-height:1px;">&nbsp;</td></tr>
      <tr><td style="height:32px;">&nbsp;</td></tr>

      <!-- Footer -->
      <tr><td style="text-align:center; padding:24px 0 48px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td align="center" style="font-family:'Bree Serif',Georgia,serif; font-size:14px; color:#FF6343; padding-bottom:8px;">Step outside your algorithm.</td></tr>
          <tr><td align="center" style="font-family:'DM Sans',sans-serif; font-size:12px; color:#555; padding-bottom:20px;">
            See all 257 voices at
            <a href="https://newsreel-perspectives.onrender.com" style="color:#FF6343; text-decoration:none;">newsreel-perspectives.onrender.com</a>
          </td></tr>
          <tr><td align="center" style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#333; text-transform:uppercase; letter-spacing:1px;">
            Newsreel &middot; The Mirror v8
          </td></tr>
        </table>
      </td></tr>

    </table>
  </td></tr>
</table>

</body>
</html>'''

    return html


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("\n  The Mirror v8 - Perspective Profiles (rebuilt from scratch)")
    print("  " + "=" * 55 + "\n")

    # Load data
    users = json.loads(USERS_PATH.read_text())
    voices = json.loads(VOICES_PATH.read_text())

    # Load poll responses (handle the nested Supabase format)
    with open("/tmp/poll-responses-all.json") as f:
        raw_data = json.load(f)

    inner = json.loads(raw_data[0]["text"])
    result_str = inner["result"]
    match = re.search(r'<untrusted-data-[^>]+>\n(.*?)\n</untrusted-data', result_str, re.DOTALL)
    if not match:
        raise ValueError("Could not parse poll responses from Supabase format")
    all_responses = json.loads(match.group(1).strip())

    print(f"  Loaded {len(users)} users, {len(all_responses)} poll responses, {len(voices)} voices\n")

    # STEP 1: Classify all questions
    question_index = build_question_index(all_responses)
    print(f"  Classified {len(question_index)} unique questions")

    # Print classification stats
    direction_counts = Counter(qi["direction"] for qi in question_index.values())
    topic_counts = Counter(qi["topic"] for qi in question_index.values())
    print(f"  Directions: {dict(direction_counts)}")
    print(f"  Topics: {dict(sorted(topic_counts.items(), key=lambda x: -x[1]))}")

    # STEP 2: Compute question averages
    question_avgs = compute_question_averages(all_responses)

    # Index responses by user
    user_responses_map = defaultdict(list)
    for r in all_responses:
        user_responses_map[r["user_id"]].append(r)

    # STEP 3: Build voice profiles
    voice_profiles = {}
    classified_voices = 0
    for v in voices:
        profile = build_voice_profile(v)
        if profile["classified"]:
            voice_profiles[v["id"]] = {"voice": v, "profile": profile}
            classified_voices += 1

    print(f"\n  Classified {classified_voices} / {len(voices)} voices for matching")

    # STEP 4 & 5: Process each user
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    profiles_generated = 0
    all_closest_voices = []
    jack_html = None

    for user in users:
        uid = user["id"]
        uresp = user_responses_map.get(uid, [])
        if not uresp:
            print(f"  [SKIP] {user['first_name']} - no responses found")
            continue

        # Build user profile
        user_profile = build_user_profile(uresp, question_index)
        if not user_profile:
            print(f"  [SKIP] {user['first_name']} - no classifiable responses")
            continue

        # Match to voices
        closest, different = match_user_to_voices(user_profile, voice_profiles)

        # Generate HTML
        is_jack = uid == "bc462c42-b880-40ee-aebe-ec8562053fd5"
        html = generate_profile_html(user, user_profile, closest, different, question_avgs, is_jack=is_jack)

        if is_jack:
            jack_html = html

        # Write file
        profile_path = PROFILES_DIR / f"{uid}.html"
        with open(profile_path, "w") as f:
            f.write(html)

        profiles_generated += 1

        if closest:
            all_closest_voices.append(closest[0]["voice"]["name"])

        name = (user["first_name"] or "").strip()
        score = user_profile["overall_score"]
        stance = stance_label(score)
        if closest:
            top = closest[0]["voice"]["name"]
            sim = int(closest[0]["similarity"] * 100)
            print(f"  [{profiles_generated:2d}] {name:12s} score={score:.2f} ({stance}) top={top} ({sim}%)")
        else:
            print(f"  [{profiles_generated:2d}] {name:12s} score={score:.2f} ({stance}) no match")

    # Summary
    print(f"\n  {'=' * 50}")
    print(f"  SUMMARY")
    print(f"  {'=' * 50}\n")
    print(f"  Profiles generated: {profiles_generated}")
    print(f"  Output directory: {PROFILES_DIR}\n")

    voice_counts = Counter(all_closest_voices)
    print(f"  Most common top match:")
    for voice, count in voice_counts.most_common(10):
        print(f"    {voice}: {count} users")

    avg_score = sum(
        build_user_profile(user_responses_map.get(u["id"], []), question_index)["overall_score"]
        for u in users
        if user_responses_map.get(u["id"]) and build_user_profile(user_responses_map.get(u["id"], []), question_index)
    ) / max(1, profiles_generated)
    print(f"\n  Average normalized score: {avg_score:.3f}")
    print(f"  (0 = very conservative, 0.5 = center, 1 = very progressive)")

    # Save Jack's HTML for sending
    if jack_html:
        jack_path = PROFILES_DIR / "jack-v8.html"
        with open(jack_path, "w") as f:
            f.write(jack_html)
        print(f"\n  Jack's profile saved to: {jack_path}")

    print(f"\n  Done!\n")


if __name__ == "__main__":
    main()
