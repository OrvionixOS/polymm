"""
Canonical match ID generation and team normalization.

This module provides THE ONLY functions that should be used for:
1. Normalizing team names
2. Normalizing game names  
3. Creating match IDs

DO NOT implement these functions elsewhere. Import from here.
"""

import re
import unicodedata

from src.core.first_names import FIRST_NAMES


# ============================================================================
# TEAM ALIASES: Canonical name -> list of known variations
# NOTE: All names should be lowercase, no accents, no spaces/punctuation
# Only add aliases when names are GENUINELY different (not just formatting)
# ============================================================================
TEAM_ALIASES = {
    # Brazilian teams - different actual names
    "vivokeydstars": ["keydstars", "vivokeyd"],  # "Keyd Stars" = "Vivo Keyd Stars"
    "fluxow7m": ["fluxo"],                        # "Fluxo" = "Fluxo W7M"
    
    # Chinese teams - typos/variations
    "weibogaming": ["weibo", "weibotursogaming", "weiboturnso", "weiboturnsogaming"],
    "ohmygod": ["omg"],  # "OMG" = "Oh My God" (Chinese LoL team)
    "shenzhenpengcity": ["shenzhenxinpengcheng", "shenzhenxinpengchengfc"],  # Shenzhen rebrand
    
    # MLBB teams - abbreviations
    "dianfengyaoguai": ["dfyg"],  # "DFYG" = "DianFengYaoGuai"
    
    # European teams - different branding
    "futacademy": ["futesportsacademy"],          # "FUT Esports Academy" = "FUT Academy"
    
    # Common abbreviations/rebrands
    "navi": ["natusvincere"],
    "vp": ["virtuospro", "virtuspro"],
    "nip": ["ninjasinpyjamas"],
    "eg": ["evilgeniuses"],
    "liquid": ["teamliquid"],
    "spirit": ["teamspirit"],
    "secret": ["teamsecret"],
    "g2": ["g2esports"],
    "vitality": ["teamvitality"],
    
    # Dota 2 teams - abbreviations/rebrands
    "1win": ["1w"],  # "1w" = "1win"
    
    # Korean teams - Academy/Challengers rosters
    # In Korean esports, "Challengers" teams are often referred to without the suffix
    "hanjinbrionchallengers": ["hanjinbrion"],  # "Hanjin Brion" = "HANJIN BRION Challengers"
    "t1academy": ["t1esportsacademy"],  # "T1 Esports Academy" = "T1 Academy"
    "nongshimesportsacademy": ["nongshimea", "nongshimacademy"],  # "Nongshim.Ea" / "Nongshim Academy" = "Nongshim Esports Academy"
    "dnsooperschallengers": ["dnsoopers"],  # "Dn Soopers" = "DN SOOPers Challengers"
    "dpluskiachallengers": ["dpluschallengers"],  # "Dplus Challengers" = "Dplus KIA Challengers"
    "bnkfearxyouth": ["bnkfearx", "fearx", "fearxyouth"],  # "BNK FearX" / "FearX" / "FearX Youth" = "BNK FearX Youth"
    "hanwhalifechallengers": ["hanwhalifeesportschallengers"],  # "Hanwha Life Esports Challengers" = "Hanwha Life Challengers"
    "kiaesubaacademy": ["esuba", "kiaesuba"],  # "eSuba" / "KIA eSuba" = "KIA.eSuba Academy" (Czech LoL team)
    "drxprospects": ["drxacademy"],  # "DRX Academy" (source_g) = "DRX Prospects" (Polymarket) — DRX secondary Valorant roster (VCL)
    "faze": ["fazeclan"],  # "FaZe Clan" (source_g) = "FaZe" (source_i/Polymarket)

    # European Academy/Blue rosters
    "karminecorpblue": ["karminecorpacademy"],  # "Karmine Corp Academy" = "Karmine Corp Blue"
    
    # Regional team variations (Turkey, Philippines, etc.)
    # Aurora Gaming is a special case - "Gaming" is part of the official name, not a suffix
    # After parenthetical stripping:
    # - "Aurora Gaming (Turkiye)" → "aurora gaming" → becomes "aurora" after suffix removal
    # - "Aurora Gaming" → "aurora gaming" → becomes "aurora" after suffix removal  
    #
    # Solution: Explicitly alias "aurora" to "auroragaming" to preserve the full name
    "auroragaming": ["aurora", "auroraturkiye"],  # Aurora Gaming / Aurora Türkiye (same team, preserve "Gaming")
    "liquidph": ["teamliquidph"],  # Team Liquid PH -> liquidph (keep as different from main Team Liquid)
    "aurorayoungblood": ["aurorayoungblud"],  # "Aurora Young Blud" typo = "Aurora Young Blood"
    
    # Dota 2 specific teams
    "lookingfororg": ["lookingfororgpe"],  # "Looking For Org PE" = "Looking for Org"
    
    # CS2 teams - common abbreviations and variations
    "senshiesportsclub": ["senshiesports", "senshi"],  # "Senshi eSports" = "Senshi Esports Club"
    "sangalalters": ["sangalacademy"],  # "Sangal Academy" = "Sangal ALTERS"
    "pcificespor": ["pcific"],  # "PCIFIC" = "PCIFIC Espor"
    "watermelon": ["watermel0n"],  # "WATERMEL0N" (with zero) = "WATERMELON"
    "painacademy": ["paingamingacademy", "painacademy"],  # "paiN Gaming Academy" = "paiN Academy"
    "thebandits": ["banditsesc", "bandits"],  # "banditsesc" = "The Bandits"
    "fritesesportsclub": ["frites"],  # "Frites" = "Frites Esports Club"
    "dragonsesports": ["dragonsesportsclub"],  # "Dragons Esports Club" = "Dragons Esports" (canonical)
    
    # German teams
    # "Dortmund Esports" -> "dortmund" (suffix stripped)
    # "Dortmund Gesichtenhausen" -> "dortmundgesichtenhausen"
    "dortmund": ["dortmundgesichtenhausen"],
    
    # French teams - Academy/Bee rosters (LEC Challengers)
    # NOTE: do NOT alias bare "vitality" -> "vitalitybee". "Vitality.Bee" and
    # "Vitality Bee" already normalize to "vitalitybee" via punctuation strip,
    # while "Team Vitality"/"Vitality" (the tier-1 team) must stay "vitality".
    # The old `"vitalitybee": ["vitality"]` collapsed the main roster into the
    # academy bucket, so main-vs-NAVI and Bee-vs-NAVI got the SAME match_id —
    # a silent cross-contamination of the tier-1 team's odds with the academy's.
    # Karmine Corp has multiple names: Academy, Blue, Blue Stars - all same team
    "karminecorpbluestars": ["karminecorpblue", "karminecorpacademy"],
    
    # Brazilian teams - sponsor variations
    # "Imperial Sportsbet" -> "imperialsportsbet", "Imperial" -> "imperial"
    "imperial": ["imperialsportsbet"],
    
    # Korean teams - Academy/Challengers rosters (additional)
    "t1": ["t1academy"],  # T1 Academy often listed without "Academy" for main matches
    "ktrolster": ["ktrolsterchallengers"],  # KT Rolster Challengers
    
    # Sponsored team name variations
    # "Apogee" -> "apogee", "Betclic Apogee Esports" -> "betclicapogee" (esports stripped)
    "apogee": ["betclicapogee"],
    
    # "BoostGate Esports" -> "boostgate" (suffix stripped)
    # "BoostGate Espor" -> "boostgateespor" (espor not in suffix list)
    "boostgate": ["boostgateespor"],
    
    # Japanese teams
    # "DetonatioN FocusMe" -> "detonationfocusme", "DetonatioN FM" -> "detonationfm"
    "detonationfocusme": ["detonationfm"],
    
    # North American teams
    "shopifyrebellion": ["shopifyrebellionblack"],  # "Shopify Rebellion Black" = "Shopify Rebellion"
    
    # Dota 2 teams with suffixes
    # "Pipsqueak+4" keeps the +4 because + isn't stripped
    "pipsqueak": ["pipsqueak+4"],
    
    # Portuguese/Spanish teams
    # "Famalicao" -> "famalicao", "FC Famalicão Esports" -> "fcfamalicao" (accent removed, esports stripped)
    "famalicao": ["fcfamalicao"],
    
    # Moroccan teams
    # "GnG Esports" -> "gng" (suffix stripped), "GnG Amazigh" -> "gngamazigh"
    "gng": ["gngamazigh"],
    
    # Honor of Kings teams (Chinese)
    # "WanZhen" -> "wanzhen", "WanZhen Esports Club" -> "wanzhenesportsclub"
    "wanzhen": ["wanzhenesportsclub"],
    # "LGD Gaming" -> "lgd" (suffix stripped), "LGD NBW" -> "lgdnbw"
    "lgd": ["lgdnbw"],
    # "TOP Esports Armor" -> "topesportsarmor", "TESA TOP Esports Armor" -> "tesatopesportsarmor"
    "topesportsarmor": ["tesatopesportsarmor"],
    # "Weibo Gaming" -> "weibo" (suffix stripped), "WB Weibo Gaming" -> "wbweibo" (suffix stripped)
    # "Weibo Turnso Gaming" -> "weiboturnso" (suffix stripped) - bookmaker uses different spelling
    "weibo": ["wbweibo", "weiboturnso"],
    # "Dplus KIA" -> "dpluskia", "Dplus" -> "dplus" - Korean org with sponsor name
    "dplus": ["dpluskia"],
    # "WLT" -> "wlt", "WLT Esports Club" -> "wltesportsclub"
    "wlt": ["wltesportsclub"],
    # "XQG" -> "xqg", "XQG Esports Club" -> "xqgesportsclub"
    "xqg": ["xqgesportsclub"],
    
    # Note: paiN/pain, RED/Red, MASONIC/Masonic etc. match naturally via lowercase
    # Note: Leviatán/Leviatan match naturally via accent removal
    
    # ============================================================================
    # VALORANT GAME CHANGERS TEAMS
    # Polymarket often lists teams with "GC" suffix while odds providers don't
    # ============================================================================
    "myvragc": ["myvra"],  # "MYVRA" = "MYVRA GC"
    "gentlematesgc": ["gentlemates"],  # "Gentle Mates" = "Gentle Mates GC"
    "joblifegc": ["joblife"],  # "Joblife" = "Joblife GC"
    "bonbonbumcontragc": ["bonbonbumcontra", "contragc"],  # "Bon Bon Bum Contra" / "Contra GC" = "Bon Bon Bum Contra GC"
    
    # ============================================================================
    # RUGBY TEAM ALIASES
    # Odds providers often add "RC" (Rugby Club), "RFC" (Rugby Football Club) etc.
    # while Polymarket uses cleaner names
    # ============================================================================
    
    # English Premiership Rugby
    "bristolbears": ["bristolrcbears", "bristol"],  # Bristol RC Bears = Bristol Bears
    "exeterchiefs": ["exeter"],
    "leicestertigers": ["leicester"],
    "saracens": ["saracensrc", "saracensrfc"],
    "harlequins": ["harlequinsrc", "quins"],
    "bath": ["bathrc", "bathrugby"],
    "gloucester": ["gloucesterrc", "gloucesterrugby"],
    "newcastlefalcons": ["newcastle"],
    "northamptonsaints": ["northampton", "saints"],
    "saleharks": ["sale", "salesharks"],
    
    # French Top 14
    "stfrancaisparis": ["stadefrancaisparis", "stadefrancais", "stadefrançaisparis"],
    "toulon": ["rctoulonnais", "rctoulonnais", "rctoulon"],
    "laochelle": ["staderochelais", "stadelarochelle", "rochelle"],
    "toulouse": ["stadetoulousain"],
    "clermont": ["asmclermontauvergne", "asmclermont", "clermontauvergne", "clermontfoot63", "clermontfoot"],
    "lyon": ["lourugby", "lyonrugby"],  # LOU Rugby
    "montpellier": ["montpellierherault", "montpellierheraultrugby", "montpellierhr"],
    "bordeaux": ["unionbordeauxbegles", "ubb"],
    "castres": ["castressolympique", "castresoly"],
    "racing92": ["racingmetro92", "racingmetro", "racing"],
    "bayonne": ["avironbayonnais", "avironbayonne"],
    "pau": ["sectionpaloise", "section"],
    "perpignan": ["usaperpignan", "usap"],
    
    # United Rugby Championship
    "leinster": ["leinsterrugby"],
    "munster": ["munsterrugby"],
    "ulster": ["ulsterrugby"],
    "connacht": ["connachtrugby"],
    "cardiff": ["cardiffrugby", "cardiffblues"],
    "ospreys": ["ospreysrugby"],
    "scarlets": ["scarletsrugby"],
    "dragonsrugby": ["dragonsrfc"],  # Dragons RFC (rugby) - bare "dragons" stays as-is for esports
    "edinburgh": ["edinburghrugby"],
    "glasgow": ["glasgowwarriors"],
    "stormers": ["stormersrugby", "dhlstormers"],
    "bulls": ["vodacombluebulls", "bluebulls"],
    "sharks": ["sharksdurban", "cellcsharks"],  # Not to be confused with Sharks Esports
    "lions": ["lionsrugby", "goldenlions"],
    
    # ============================================================================
    # NCAAB TEAM ALIASES
    # Positions API returns full names with state qualifiers like "(OH)", "(NC)", "(PA)"
    # while Polymarket questions use shorter names like "Miami", "Queens", "St. Francis"
    # ============================================================================
    "miami": ["miamiredhawks"],             # Miami (OH) RedHawks → Miami
    "queens": ["queensroyals"],             # Queens (NC) Royals → Queens
    "stfrancis": ["stfrancisredflash"],     # St. Francis (PA) Red Flash → St. Francis
    "stthomas": ["stthomastommies"],         # St. Thomas (MN) Tommies → St. Thomas
    
    # ============================================================================
    # NBA TEAM ALIASES
    # Polymarket uses short nicknames, odds-api uses full city+team names
    # ============================================================================
    "atlantahawks": ["hawks"],
    "bostonceltics": ["celtics"],
    "brooklynnets": ["nets"],
    "charlottehornets": ["hornets"],
    "chicagobulls": ["bulls"],          # Note: also rugby "bulls" alias above — context resolves via game filter
    "clevelandcavaliers": ["cavaliers", "cavs"],
    "dallasmavericks": ["mavericks", "mavs"],
    "denvernuggets": ["nuggets"],
    "detroitpistons": ["pistons"],
    "goldenstatewarriors": ["warriors"],
    "houstonrockets": ["rockets"],
    "indianapacers": ["pacers"],
    "losangelesclippers": ["clippers", "laclippers"],
    "losangeleslakers": ["lakers", "lalakers"],
    "memphisgrizzlies": ["grizzlies"],
    "miamiheat": ["heat"],
    "milwaukeebucks": ["bucks"],
    "minnesotatimberwolves": ["timberwolves", "wolves"],
    "neworleanspelicans": ["pelicans"],
    "newyorkknicks": ["knicks"],
    "oklahomacitythunder": ["thunder"],
    "orlandomagic": ["magic"],
    "philadelphia76ers": ["76ers", "sixers"],
    "phoenixsuns": ["suns"],
    "portlandtrailblazers": ["trailblazers", "blazers"],
    "sacramentokings": ["kings"],
    "sanantoniospurs": ["spurs"],
    "torontoraptors": ["raptors"],
    "utahjazz": ["jazz"],
    "washingtonwizards": ["wizards"],
    
    # ============================================================================
    # NHL TEAM ALIASES
    # Same pattern: Polymarket uses short nicknames
    # ============================================================================
    "anaheimducks": ["ducks"],
    "arizonacoyotes": ["coyotes"],
    "bostonbruins": ["bruins"],
    "buffalosabres": ["sabres"],
    "calgaryflamers": ["flames", "calgaryflames"],
    "carolinahurricanes": ["hurricanes", "canes"],
    "chicagoblackhawks": ["blackhawks"],
    "coloradoavalanche": ["avalanche", "avs"],
    "columbusbluejackets": ["bluejackets"],
    "dallasstars": ["stars"],
    "detroitredwings": ["redwings"],
    "edmontonoilers": ["oilers"],
    "floridapanthers": ["panthers"],
    "losangeleskings": ["lakings"],
    "minnesotawild": ["wild"],
    "montrealcanadiens": ["canadiens", "habs"],
    "nashvillepredators": ["predators", "preds"],
    "newjerseydevils": ["devils"],
    "newyorkislanders": ["islanders"],
    "newyorkrangers": ["rangers"],
    "ottawasenators": ["senators", "sens"],
    "philadelphiaflyers": ["flyers"],
    "pittsburghpenguins": ["penguins", "pens"],
    "sanjosesharks": ["sjsharks"],
    "seattlekraken": ["kraken"],
    "stlouisblues": ["blues"],
    "tampabaylightning": ["lightning", "bolts"],
    "torontomapleleafs": ["mapleleafs", "leafs"],
    "utahhockeyclub": ["utah"],
    "vancouvercanucks": ["canucks"],
    "vegasgoldenknights": ["goldenknights", "knights"],
    "washingtoncapitals": ["capitals", "caps"],
    "winnipegjets": ["jets"],
    
    # ============================================================================
    # EUROLEAGUE BASKETBALL ALIASES
    # ============================================================================
    "fenerbahcesk": ["fenerbahce"],
    "asmonaco": ["monaco", "asmonacobasket"],
    "maccabitelaviv": ["maccabi", "maccabitelavivbc"],
    "hapoeltelaviv": ["hapoel", "hapoeltelavivbc"],
    "pallacanestroolimpiamilano": ["olimpiamilano", "armaniexchangemilano", "axmilano"],
    "fcbarcelonabasquet": ["barcelonabasquet", "barcelona", "fcbarcelona"],
    "realmadridsbaloncesto": ["realmadridbaloncesto"],
    "virtussegafredobologna": ["virtusbologna", "virtus"],
    "zalgiris": ["zalgiriskaunas"],
    "valenciabasket": ["valencia"],
    "bayernmunichbasketball": ["bayernmunichbasket"],
    "partizanbelgrade": ["partizan"],
    "crvenazvezda": ["crvenazvezdabelgrade", "redstar", "kkcrvenazvezda"],
    "paabordo": ["pao", "panathinaikos"],
    "ldynaudolympiacos": ["olympiacos", "olympiacosbc"],
    "albaberlin": ["alba"],
    "saskibaskonia": ["baskonia", "cazkilcebaskonia"],
    "parisbasketball": ["parisbasket"],
    "fcbayernmunchen": ["fcbayern"],
    
    # ============================================================================
    # FOOTBALL / SOCCER ALIASES
    # Country name differences and club variations
    # ============================================================================
    "turkiye": ["turkey"],
    # "bosnia&herzegovina" can never be a lookup key (normalize strips "&");
    # add the short forms books actually use for FIBA WCQ.
    "bosniaandherzegovina": ["bosniaherzegovina", "bosnia", "bih"],
    "czechrepublic": ["czechia", "czech"],
    "southkorea": ["korea", "korearepublic"],
    
    # Italian clubs
    "intermilan": ["intermilanfc", "inter", "fcinter", "internazionale", "fcinternazionalemilano", "fcinternazianalemilano"],
    "acmilan": ["milan", "acmilanfc"],
    "asroma": ["roma", "asromafc"],
    "sscnapoli": ["napoli", "napolifc"],
    "juventus": ["juventusfc"],
    "atalantabc": ["atalanta", "atalantabergamasca"],
    
    # Spanish clubs
    "atleticomadrid": ["atleticodemadrid", "atleticomadridfc", "clubatleticodemadrid"],
    "realmadrid": ["realmadridcf"],
    "athleticbilbao": ["athleticclub"],
    "realsociedaddefutbol": ["realsociedad"],
    "cadiz": ["cadizcf"],
    
    # French clubs
    "olympiquelyonnais": ["lyon", "olympiqlyon"],
    "olympiquedemarseille": ["marseille", "olympiqmarseille"],
    "parissaintgermain": ["psg", "parissaintgermainfc"],
    "rclens": ["lens", "racinglens"],
    "assaintetienne": ["saintetienne"],
    
    # German clubs  
    "bayerleverkusen": ["bayer04leverkusen", "bayerleverkusenfc"],
    "bayernmunich": ["bayernmunchen", "fcbayernmunchen", "fcbayern"],
    "borussiadortmund": ["bvb", "bvborussia09dortmund", "borussia09dortmund"],
    "borussiamonchengladbach": ["monchengladbach", "gladbach"],
    "herthabsc": ["herthaberlin", "herthabscberlin"],
    "scfreiburg": ["freiburg"],
    "1fcnurnberg": ["nurnberg"],
    
    # English clubs with FC variations
    "westhamunited": ["westham", "westhamunitedfc"],
    "newcastleunited": ["newcastleunitedfc"],
    "manchestercity": ["mancity", "manchestercityfc"],
    "manchesterunited": ["manutd", "manchesterunitedfc"],
    "crystalpalace": ["crystalpalacefc"],
    "tottenhamhotspur": ["tottenham", "tottenhamhotspurfc"],
    "arsenal": ["arsenalfc"],
    "chelsea": ["chelseafc"],
    "fulham": ["fulhamfc"],
    "wrexham": ["wrexhamafc"],
    "leedsunited": ["leeds", "leedsunitedfc"],
    "norwichcity": ["norwich", "norwichcityfc"],
    "southampton": ["southamptonfc"],
    
    # Scottish clubs
    "aberdeen": ["aberdeenfc"],
    "celtic": ["celticfc"],
    "dundeefc": ["dundee"],
    "dundeeunited": ["dundeeunitedfc"],
    "motherwellfc": ["motherwell"],
    "stmirren": ["stmirrenfc"],
    "rangers": ["rangersfc"],
    
    # Italian Serie A
    "udinese": ["udinesecalcio"],  # "Udinese Calcio" → "udinese" (the-odds-api uses short name)
    
    # Chilean clubs
    "colocolo": ["csdcolocolo"],
    "deporteslimache": ["cdlimache"],
    "nublense": ["cdnublense"],
    
    # Saudi clubs
    "alkholood": ["alkholoodsaudiclub"],
    "alqadsiah": ["alqadisiyahsaudiclub", "alqadisiyah"],
    
    # Australian A-League
    "newcastlejets": ["newcastlejetsfc", "newcastleunitedjetsfc", "newcastleunitedjets"],
    "westernsydneywanderers": ["westernsydneywanderersfc"],
    "centralcoastmariners": ["centralcoastmarinersfc"],
    "macarthur": ["macarthurfc"],
    
    # Korean K League
    "jejuunited": ["jejuskfc", "jejuunitedfc", "jejusk", "jeju"],
    "sangjusangmu": ["gimcheonsangmufc", "sangjusangmufc", "gimcheonsangmu"],  
    "jeonbukhyundaimotors": ["jeonbukhyundaimotorsfc"],
    "ulsanhyundai": ["ulsanhyundaifc", "ulsanhdfc", "ulsanhd"],
    "fcseoul": ["seoul"],
    
    # Dutch clubs
    "azalkmaar": ["az"],
    
    # ============================================================================
    # NCAAB ABBREVIATION FIXES
    # Odds-api abbreviates "State" as "St" while Polymarket uses full name
    # ============================================================================
    "portlandstatevikings": ["portlandstvikings"],
    "sacramentostatehornets": ["sacramentosthornets"],
    "southeasternlouisianalions": ["selouisianalions"],
    "montanastatebobcats": ["montanastbobcats"],
    "northwesternstatedemons": ["northwesternstdemons"],
    "ncstatewolfpack": ["northcarolinastatewolfpack"],
    "delawarestatehornets": ["delawaresthornets"],
    "southcarolinastatebulldogs": ["southcarolinastbulldogs"],
    "clevelandstatevikings": ["clevelandstvikings"],
    "morganstatebears": ["morganstbears"],
    "norfolkstatespartans": ["norfolkstspartans"],
    "coppinstateagles": ["coppinsteagles"],
    "nicholssstatecolonels": ["nichollsstcolonels"],
    "mcneesestatecowboys": ["mcneesecowboys"],
    
    # Additional State→St abbreviations from diagnostic
    "fresnostatebulldogs": ["fresnostbulldogs"],
    "arizonastatesundevils": ["arizonastsundevils"],
    "mississippistatebulldogs": ["mississippistbulldogs"],
    "kansasstatewildcats": ["kansasstwildcats"],
    "gramblingstatetigers": ["gramblingsttigers"],
    "jacksonstatetigers": ["jacksonsttigers"],
    "alabamastatehornets": ["alabamasthornets"],
    "sandiegostateaztecs": ["sandiegostaztecs"],
    "sanjosestatespartans": ["sanjosestspartans"],
    "oklahomastatecowboys": ["oklahomastcowboys"],
    "idahostatebengals": ["idahostbengals"],
    "alcornstatebraves": ["alcornstbraves"],
    "georgistatepanthers": ["georgiastpanthers"],
    
    # Non-St abbreviations
    "louisianamonroewarhawks": ["ulmonroewarhawks"],
    "massachusettslowellriverhawks": ["umasslowellriverhawks"],
    "mississippivalleystatedeltadevils": ["missvalleystdeltadevils"],
    "northerncoloradobears": ["ncoloradobears"],
    "armyblackknights": ["armyknights"],
    "unlvrunninrebels": ["unlvrebels"],
    "uncwseahawks": ["uncwilmingtonseahawks"],
    "miamihurricanes": ["miamiredhawks"],  # Different teams but same normalization needed
    "georgiatechyellowjackets": ["georgiastpanthers", "georgiatechyellowjackets"],
    
    # Additional State→St abbreviations
    "youngstownstatepenguins": ["youngstownstpenguins"],
    "centralconnecticutstatebluedevils": ["centralconnecticutstbluedevils"],
    "chicagostatecougars": ["chicagostcougars"],
    "floridastateseminoles": ["floridastseminoles"],
    
    # NCAAB — name abbreviations/variants
    "siuedwardsvillecougars": ["siuecougars"],  # SIUE = Southern Illinois Univ. Edwardsville
    "loyolachicagoramblers": ["loyolaramblers"],  # Odds-api drops "Chicago"
    "gardnerwebbbulldogs": ["gardnerwebbrunninbulldogs"],  # Polymarket adds "Runnin'"
    "southcarolinaupstatespartans": ["uscupstatespartans"],  # USC Upstate = South Carolina Upstate
    
    # NCAAB teams with other name variations
    "bostonterriers": ["bostonuniversityterriers"],
    "detroittitans": ["detroitmercytitans"],
    "charlestoncougars": ["collegeofcharlestoncougars"],
    "providencefriars": ["providencecollegefriars"],
    "notredamefightingirish": ["notredameirish"],
    "pittsburghpanthers": ["pittpanthers"],
    "iowahawkeyes": ["iowahawkeyeshawkeyes"],
    "baylorbears": ["bayloruniversitybears"],
    "villanovawildcats": ["villanovauniversitywildcats"],
    "coloradostaterams": ["coloradostrams"],
    "stanfordcardinal": ["stanforduniversitycardinal"],
    "californiagoldenbears": ["calbears", "calgoldenbears"],
    "bowlinggreenfalcons": ["bowlinggreenstatebowlinggreenfalcons", "bgsufalcons"],
    "washingtonhuskies": ["uwashingtonhuskies"],
    "houstoncougars": ["houstonuniversitycougars"],
    "colgateraiders": ["colgateuniversityraiders"],
    "butlerbulldogs": ["butleruniversitybulldogs"],
    "georgewashingtonrevolutionaries": ["gwurevolutionaries", "gwrevolutionaries"],
    "binghamtonbearcats": ["binghamtonuniversitybearcats"],
    "oaklandgoldengrizzlies": ["oaklanduniversitygoldengrizzlies"],
    "southeastmelbournephoenix": ["semelbournephoenix"],
    
    # ============================================================================
    # AHL / SHL HOCKEY ALIASES
    # ============================================================================
    "hersheybears": ["hershey"],
    "charlottecheckers": ["charlotte"],

    # AHL — abbreviations
    "wilkesbarrescrantonpenguins": ["wbscrantonpenguins"],
    
    # SHL — Swedish Hockey League
    "orebrohk": ["oerebrohk"],
    "djurgardensif": ["djurgaarden", "djurgarden"],

    # ============================================================================
    # SPORTS TEAM ALIASES — Full legal names from Polymarket
    # Polymarket uses FIFA/official full legal names for many clubs
    # ============================================================================

    # La Liga
    "realbetis": ["realbetisbalompie"],
    "celtavigo": ["rcceltadevigo", "celtadevigofc"],

    # Bundesliga
    "hoffenheim": ["tsg1899hoffenheim", "tsghoffenheim"],
    "heidenheim": ["1fcheidenheim1846", "1fcheidenheim"],
    "koln": ["1fckoln"],
    "wolfsburg": ["vflwolfsburg"],
    "augsburg": ["fcaugsburg"],
    "fcstpauli": ["fcstpauli1910", "stpauli1910", "stpauli"],

    # 2. Bundesliga
    "fortunadusseldorf": ["tsvfortuna95dusseldorf", "fortuna95dusseldorf"],
    "scpaderborn": ["scpaderborn07"],
    "elversberg": ["sv07elversberg"],
    "greutherfurth": ["spvgggreutherfurth"],
    "dynamodresden": ["sgdynamodresden"],
    "preussenmunster": ["scpreußenmunster", "scpreussenmunster"],
    "arminiabielefeld": ["dscarminiabielefeld"],
    "kaiserslautern": ["1fckaiserslautern"],
    "magdeburg": ["1fcmagdeburg"],
    "darmstadt": ["svdarmstadt98"],
    "holsteinkiel": ["kiel"],
    "schalke": ["fcschalke04", "schalke04"],
    "hannover": ["hannover96"],
    "karlsruher": ["karlsruhersc"],
    "bochum": ["vflbochum"],

    # Ligue 1
    "rennes": ["staderennaisfc1901", "staderennais"],
    "lens": ["racingclubdelens", "rclens", "racinglens"],
    "nice": ["ogcnice"],
    "lille": ["lilleosc", "losc"],
    "lorient": ["fclorient"],
    "metz": ["fcmetz"],

    # Ligue 2
    "guingamp": ["enavantguingamp"],
    "nancy": ["asnancylorraine"],
    "bastia": ["scbastia"],
    "laval": ["stadelavalloismayenne", "stadelavallois"],
    "reims": ["stadedereims"],
    "grenoble": ["grenoblefoot38"],
    "annecy": ["fcannecy"],
    "troyes": ["estroyesac"],
    "rodez": ["rodezaveyronfootball"],
    "boulogne": ["usboulognecotedopale"],
    "dunkerque": ["usldunkerque"],
    "lemans": ["lemansfc"],
    "montpellierhsc": ["montpellier"],


    # EPL
    "brighton": ["brightonhovealbion", "brighton&hovealbion", "brightonandhovealbion"],
    "sunderland": ["sunderlandafc"],

    # MLS
    "lagalaxy": ["losangelesgalaxy", "lagalaxyfc"],
    "lafc": ["losangelesfc"],
    "sportingkc": ["sportingkansascity"],
    "sanjose": ["sanjoseearthquakes"],
    "dcunited": ["dcunitedsc"],
    "newyorkredbulls": ["nyredbulls"],
    "newyorkcity": ["newyorkcityfc", "nycfc"],
    "coloradorapids": ["coloradorapidssc"],
    "orlandocity": ["orlandocitysc"],
    "nashville": ["nashvillesc"],
    "intermiami": ["inthermiamicf"],
    "cfmontreal": ["montreal"],
    "stlouiscity": ["stlouiscitysc"],
    "sandiego": ["sandiegofc"],
    "atlanta": ["atlantaunited", "atlantaunitedfc"],
    "philadelphiaunion": ["philadelphia"],
    "chicagofire": ["chicagofirefc"],
    "columbuscrew": ["columbus"],
    "newenglandrevolution": ["newengland"],
    "fccincinnati": ["cincinnati"],
    "vancouverwhitecaps": ["vancouverwhitecapsfc"],
    "houstondynamo": ["houston"],
    "portlandtimbers": ["portland"],
    "realsaltlake": ["saltlake"],
    "austin": ["austinfc"],
    "toronto": ["torontofc"],
    "fcdallas": ["dallas"],

    # Saudi Pro League — strip "Saudi Club" suffix
    "alhilal": ["alhilalsaudiclub"],
    "alnassr": ["alnassrsaudiclub"],
    "alshabab": ["alshababsaudiclub"],
    "alfateh": ["alfatehsaudiclub"],
    "alettifaq": ["alettifaqsaudiclub"],
    "altaawoun": ["altaawounsaudiclub"],
    "alriyadh": ["alriyadhsaudiclub"],
    "alkhaleej": ["alkhaleejsaudiclub"],
    "alfayha": ["alfayhasaudiclub"],
    "alahli": ["alahlisaudiclub"],
    "alittihad": ["alittihadsaudiclub"],
    "alnajma": ["alnajmahsaudiclub", "alnajmah"],
    "alhazem": ["alhazemsc"],
    "alokhdood": ["alokhdoodsc"],
    "neom": ["neomsc"],
    "damac": ["damacsaudiclub"],

    # Brasileirão
    "atleticomineiro": ["camineiro", "atleticomineirofc"],
    "athleticoparanaense": ["caparanaense", "athleticopr", "atleticoparanaense"],
    "vitoria": ["ecvitoria"],
    "internacional": ["scinternacional"],
    "bragantino": ["redbullbragantino", "bragantinosp"],
    "gremio": ["gremiofbpa"],
    "vascodagama": ["crvascodagama"],
    "cruzeiro": ["cruzeiroec"],
    "corinthians": ["sccorinthianspaulista"],
    "flamengo": ["crflamengo"],
    "palmeiras": ["sepalmeiras"],
    "bahia": ["ecbahia"],
    "saopaulo": ["saopaulofc"],
    "botafogo": ["botafogofr"],
    "santos": ["santosfc"],
    "fluminense": ["fluminensefc"],
    "coritiba": ["coritibafbc"],
    "mirassol": ["mirassolfc"],
    "chapecoense": ["associacaochapecoensedefutebol"],
    "remo": ["clubedoremo"],

    # Chilean Primera
    "universidaddechile": ["cfuniversidaddechile"],
    "universidadcatolica": ["cduniversidadcatolica"],
    "coquimbounido": ["cdcoquimbounido"],
    "huachipato": ["cdhuachipato"],
    "palestino": ["cdpalestino"],
    "laserena": ["cdlaserena"],
    "unionlacalera": ["cdunionlacalera"],
    "concepcion": ["cdconcepcion", "cduniversidaddeconcepcion"],
    "evertonvinadelmar": ["evertondevinadelmar"],
    "ohiggins": ["ohigginsfc"],

    # Liga MX
    "pachuca": ["cfpachuca"],
    "monterrey": ["cfmonterrey"],
    "cruzazul": ["cfcruzazul"],
    "america": ["cfamerica"],
    "toluca": ["deportivotoluca", "deportivotolucafc"],
    "atlas": ["atlasfc"],
    "mazatlan": ["mazatlanfc"],
    "leon": ["clubleonfc", "clubleon"],
    "tijuana": ["clubtijuana"],
    "guadalajara": ["cdguadalajara"],
    "santoslaguna": ["clubsantoslaguna"],
    "puebla": ["clubpuebla"],
    "necaxa": ["clubnecaxa"],
    "juarez": ["fcjuarez"],
    "queretaro": ["queretarofc"],
    "tigres": ["tigresdelauanl"],
    "pumas": ["pumasdelaunam"],
    "sanluis": ["atleticosanluis"],

    # Copa Sudamericana / Libertadores
    "tolima": ["cdtolima"],
    "alianzaatletico": ["clubalianzaatletico"],
    "garcilaso": ["cdgarcilaso"],
    "olimpia": ["clubolimpia"],
    "nacional": ["clubnacional"],
    "bucaramanga": ["cabucaramanga"],
    "melgar": ["fbcmelgar"],
    "cienciano": ["cscienciano"],
    "trinidense": ["cstrinidense"],
    "cristal": ["cscristal"],

    # Conf. League
    "spartapraha": ["acspartapraha"],
    "aeklarnakas": ["aeklarnaka"],

    # Primeira Liga (Portugal)
    "sportingcp": ["sporting", "sportinglisbon"],
    "scbraga": ["braga"],

    # Serie B
    "spezia": ["speziacalcio"],
    "frosinone": ["frosinonecalcio"],
    "modena": ["modenafc2018"],
    "mantova": ["mantova1911"],
    "padova": ["calciopadova"],
    "catanzaro": ["uscatanzaro1929"],
    "monza": ["acmonza"],
    "palermo": ["palermofc"],
    "sampdoria": ["ucsampdoria"],
    "venezia": ["veneziafc"],
    "juvestabia": ["ssjuvestabia"],
    "carrarese": ["carraresecalcio"],
    "bari": ["sscbari"],
    "reggiana": ["acreggiana1919"],
    "avellino": ["usavellino1912"],
    "entella": ["virtusentella"],
    "pescara": ["delfinopescara1936"],
    "sudtirol": ["fcsudtirol"],
    "cesena": ["cesenafc"],
    "empoli": ["empolifc"],

    # Eredivisie
    "twente": ["fctwente65", "fctwente"],
    "ajax": ["afcajax"],
    "feyenoord": ["feyenoordrotterdam"],
    "psveindhoven": ["psv"],
    "groningen": ["fcgroningen"],
    "heerenveen": ["scheerenveen"],
    "excelsior": ["sbvexcelsior"],
    "telstar": ["telstar1963"],

    # Süper Lig
    "fatihkaragumruk": ["fatihkaragumruksk"],
    "kasimpasa": ["kasımpasask", "kasimpasask", "kasımpasa"],

    # A-League

    # K League (NOTE: gimcheonsangmu and ulsanhdfc aliases are on lines 399/401
    # — do NOT re-add here or it will override the canonical mapping)
    "pohangsteelers": ["pohangsteelersfc"],
    "gangwon": ["gangwonfc"],
    "bucheon": ["bucheonfc1995"],
    "daejeonhanacitizen": ["daejeonhanacitizensfc", "daejeonhana"],
    "incheonunited": ["incheonunitedfc"],
    "fcanyang": ["anyang"],
    "gwangju": ["gwangjufc"],

    # Scottish Prem
    "heartofmidlothian": ["hearts"],
    "hibernian": ["hibernianfc", "hibs"],
    "livingston": ["livingstonfc"],
    "falkirk": ["falkirkfc"],
    "kilmarnock": ["kilmarnockfc"],

    # Copa del Rey
    "fcbarcelona": ["fcbarcelonabasquet", "barcelona", "barcelonafc"],

    # ============================================================================
    # SECOND ROUND — remaining containment fixes from diagnostic
    # ============================================================================

    # Brasileirão — odds-api uses different canonical names
    "atleticobucaramanga": ["cabucaramanga", "bucaramanga"],

    # Copa Sudamericana / Libertadores 
    "sportingcristal": ["cscristal", "cristal"],
    "deportestolima": ["cdtolima", "tolima"],
    "clubcienciano": ["cscienciano", "cienciano"],
    "deportivogarcilaso": ["cdgarcilaso", "garcilaso"],
    "cobresal": ["cdcobresal"],
    "audaxitaliano": ["audaxcsitaliano"],
    "americadecali": ["americacali"],
    "recoleta": ["recoletafc"],

    # Eredivisie — odds-api canonicals
    "fctwenteenschede": ["fctwente65", "twente", "fctwente"],
    "sctelstar": ["telstar1963", "telstar"],
    "necnijmegen": ["nec"],
    "fczwolle": ["peczwolle", "zwolle"],

    # J-League — odds-api uses these exact names
    "jefunitedchiba": ["jefunitedichiharachiba", "jefunited"],
    "kyotopurplesanga": ["kyotosanga", "kyotosangafc", "kyoto"],
    "mitohollyhock": ["fcmitohollyhock"],
    "yokohamafmarinos": ["yokohamaf·marinos"],
    # Teams in Polymarket but NOT in our odds-api — add placeholders for future
    "visselkobe": ["kobe"],
    "sanfreccehiroshima": ["hiroshima"],
    "kawasakifrontale": ["kawasaki"],
    "fcmachidazelvia": ["machidazelvia", "machida"],

    # K League — odds-api canonicals

    # Norwegian Eliteserien (NOT in our odds-api — genuinely unscraped)
    "bodoglimt": ["fkbodø/glimt", "bodø/glimt", "fkbodoglimt"],
    "lillestrom": ["lillestrømsk", "lillestromsk"],
    "kristiansund": ["kristiansundbk"],
    "sarpsborg": ["sarpsborg08ff", "sarpsborg08"],
    "aalesund": ["aalesundsfk"],
    "hamarkameratene": ["hamkam"],
    "tromsø": ["tromsøil"],
    "fredrikstad": ["fredrikstadfk"],
    "kfum": ["kfumkamerateneoslo"],
    "ikstart": ["start"],
    "skbrann": ["brann"],
    "sandefjord": ["sandefjordfotball"],
    "rosenborg": ["rosenborgbk"],
    "valerenga": ["valerengafotball"],
    "viking": ["vikingfk"],
    "molde": ["moldefk"],

    # Süper Lig — odds-api canonical
    "gazisehirgaziantep": ["gaziantepfk", "gaziantep"],
    "torkukonyaspor": ["konyaspor"],

    # Ligue 2 — remaining
    "rodezaf": ["rodezaveyronfootball", "rodez"],

    # Scottish Prem — fix "rangers" false match

    # Europa League / Conf League — remaining unmatched
    "ferencvaros": ["ferencvarositc"],
    "panathinaikos": ["panathinaikosao", "pao"],
    "aeklarnaka": ["aeklarnakas", "aeklanarka", "aeklarnaca"],
    "spartaprague": ["acspartapraha", "spartapraha"],

    # Chilean — concepcion disambiguation
    "universidaddeconcepcion": ["cduniversidaddeconcepcion", "concepcion"],

    # Copa Sudamericana — remaining
    "olimpiaasuncion": ["clubolimpia", "olimpia"],
    "sportivotrinidense": ["cstrinidense", "trinidense"],

    # Copa Libertadores

    # ============================================================================
    # FOURTH ROUND — final remaining fixes from diagnostic
    # ============================================================================

    # K League — suffix stripping creates "jeju" from "Jeju SK FC"

    # Saudi Pro League — one letter mismatch

    # Scottish Prem — rangers disambiguation (Glasgow Rangers)
    # Already have "rangers": ["rangersfc"] above — add note it's Glasgow
    # "falkirk" and "hibernian" etc. are NOT in our soccer_spl odds — genuinely unscraped teams

    # Conf. League

    # Süper Lig — Turkish ı→i

    # Ligue 2

    # K-League — teams NOT in our odds but map to closest canonical
    # gimcheonsangmu = sangjusangmu (renamed), already aliased above
    # bucheon, gwangju, incheonunited, daejeonhanacitizen — in odds as different names
    # ulsanhdfc = ulsanhyundai (rebranded), already aliased above
}

# Build reverse lookup: variation -> canonical
_ALIAS_TO_CANONICAL = {}
for canonical, variations in TEAM_ALIASES.items():
    _ALIAS_TO_CANONICAL[canonical] = canonical
    for var in variations:
        _ALIAS_TO_CANONICAL[var] = canonical


def normalize_team(name: str) -> str:
    """
    Normalize team name for matching and match_id generation.
    
    This is THE ONLY canonical normalization function. Do not reimplement elsewhere.
    
    Steps:
    1. Remove accents (á → a, é → e, etc.)
    2. Lowercase
    3. Strip parenthetical suffixes (e.g., "(Turkiye)", "(PH)")
    4. Remove common prefixes/suffixes
    5. Remove ALL formatting characters (spaces, hyphens, dots, underscores)
    6. Apply alias mapping
    
    Examples:
        "Team Nemesis" -> "nemesis"
        "Nemesis eSports" -> "nemesis"
        "Aurora Gaming (Turkiye)" -> "auroragaming"
        "Aurora Gaming" -> "auroragaming"
        "Karmine Corp Blue" -> "karminecorpblue"
        "paiN Gaming" -> "pain"
        "Leviatán Esports" -> "leviatan"
        "Keyd Stars" -> "vivokeydstars"
        "Vivo Keyd Stars" -> "vivokeydstars"
        "100 Thieves" -> "100thieves"
    """
    import re
    
    # Step 1: Remove accents (Leviatán → Leviatan)
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    
    # Step 2: Lowercase
    name = name.lower().strip()
    
    # Step 3: Strip mentions-specific suffixes from Polymarket position outcomes
    # Slash alternatives: "Iran / Nuclear" → "Iran" (multi-keyword mentions)
    if " / " in name:
        name = name.split(" / ")[0].strip()
    # Frequency suffixes: "Illegal Alien 5+ times" → "Illegal Alien"
    name = re.sub(r'\s+\d+\+?\s*times?$', '', name).strip()
    
    # Step 4: Strip parenthetical content (e.g., "(Turkiye)", "(PH)", "(BO3)")
    # This must happen BEFORE suffix removal to avoid keeping content from parens
    name = re.sub(r'\s*\([^)]*\)', '', name).strip()

    # Step 4a: "Last, First" → "First Last" (tennis/player books list the
    # surname first, "Sinner, Jannik"; Polymarket lists the given name first,
    # "Jannik Sinner"). Only a simple two-part personal name is swapped so an
    # org name with a comma isn't reordered. Runs before punctuation is stripped.
    if name.count(",") == 1 and ", " in name:
        last, first = name.split(", ", 1)
        if last and first:
            name = f"{first} {last}"

    # Step 4b: Strip known league/event prefixes (e.g., "AHL: ", "UFC 326: ", "Six Nations: ")
    for lp in ("ahl: ", "shl: ", "nhl: ", "khl: ", "six nations: "):
        if name.startswith(lp):
            name = name[len(lp):]
    # UFC numbered events: "UFC 326: Fighter Name" → "Fighter Name"
    name = re.sub(r'^ufc\s*\d+:\s*', '', name)
    
    # Step 4: Remove common prefixes
    for prefix in ("team ",):
        if name.startswith(prefix):
            name = name[len(prefix):]
    
    # Step 5: Remove common suffixes (but NOT "blue", "academy" - these are meaningful!)
    # Rugby suffixes: " rugby", " rc" (Rugby Club), " rfc" (Rugby Football Club)
    # Sports suffixes: " saudi club", " fotball"
    for suffix in (" esports", " gaming", " esport", " team", " gg", " e-sports", " rugby", " rc", " rfc", " fc", " afc", " sc", " cf", " saudi club", " fotball", " fk", " bk", " il", " sk"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    
    # Step 6: Preserve minus signs for negative temperatures (e.g., -3°C → neg3°c)
    # This must happen BEFORE removing hyphens, otherwise -3°C becomes 3°c
    import re
    name = re.sub(r'-(\d)', r'neg\1', name)  # -3 → neg3
    
    # Step 7: Remove ALL formatting: spaces, hyphens, dots, underscores, ampersands
    name = name.replace(" ", "").replace("-", "").replace(".", "").replace("_", "").replace("'", "").replace("&", "").replace(":", "").replace("/", "").replace("?", "")
    
    # Step 8: Apply alias mapping to canonical form
    if name in _ALIAS_TO_CANONICAL:
        name = _ALIAS_TO_CANONICAL[name]

    return name


def _normalize_token(tok: str) -> str:
    """Light per-token normalization for a doubles surname: accents, lowercase,
    strip the same punctuation set as normalize_team step 7 — but NONE of the
    mentions/suffix/alias transforms (a surname must not be stripped of a trailing
    'sc'/'fc' or aliased into an esports team)."""
    tok = unicodedata.normalize("NFD", tok)
    tok = "".join(c for c in tok if unicodedata.category(c) != "Mn")
    tok = tok.lower().strip()
    for ch in " -._'&:/?":
        tok = tok.replace(ch, "")
    return tok


def _is_initial(token: str) -> bool:
    """True if every alphabetic run in the token is a single letter — an initial
    like 'Q', 'P-H', 'P.H.' (source_i abbreviates one partner's given name)."""
    runs = re.findall(r"[a-zA-Z]+", token)
    return bool(runs) and all(len(r) == 1 for r in runs)


# First names normalized the same way tokens are (so "hao-ching" matches
# "Hao-Ching" after punctuation stripping).
_FIRST_NAMES_NORM = frozenset(_normalize_token(n) for n in FIRST_NAMES)


def _extract_surname(player: str) -> str:
    """Surname of ONE doubles player, across the three book formats:
        "Veldheer"            (Polymarket: surname only)      -> "veldheer"
        "Mick Veldheer"       (source_d: First Last)      -> "veldheer"
        "Herbert P-H"         (source_i: Surname Initial)         -> "herbert"
        "Cavalle-Reimers Y"   (source_i: compound surname)        -> "cavallereimers"
        "Charaeva, Alina"     (source_i: Surname, First)          -> "charaeva"
    """
    player = player.strip()
    if not player:
        return ""
    # "Surname, First" -> surname is before the comma.
    if "," in player:
        return _normalize_token(player.split(",")[0])
    tokens = player.split()
    if len(tokens) <= 1:
        return _normalize_token(player)
    non_initial = [t for t in tokens if not _is_initial(t)]
    if len(non_initial) == 1:
        # "Halys Q" / "Cavalle-Reimers Y" -> the single non-initial token.
        return _normalize_token(non_initial[0])
    if len(non_initial) >= 2:
        # Two+ full tokens, no initial to disambiguate. Books disagree on order
        # ("Sander Gille" First-Last vs "Gille Sander" Surname-First), so strip
        # known FIRST names. If exactly one token remains it's the surname; else
        # assume "First Last" (last token) — safest across compound FIRST names
        # ("Carlo Alberto Caniato" -> "caniato") and all-spaced compound surnames
        # ("... Van Der Merwe" -> "merwe" on every source consistently). Only a
        # hyphen-vs-space compound surname across sources ("Garcia-Perez" vs
        # "Garcia Perez") stays a harmless orphan.
        surname_toks = [t for t in non_initial if _normalize_token(t) not in _FIRST_NAMES_NORM]
        if len(surname_toks) == 1:
            return _normalize_token(surname_toks[0])
        return _normalize_token(non_initial[-1])
    return _normalize_token(player)


def canonical_pair(name: str):
    """Order- and format-independent canonical key for a tennis DOUBLES pair.
    "Ruehl/Veldheer" (PM), "Tim Ruehl/Mick Veldheer" (source_d) and
    "Ruehl X / Veldheer Y" (source_i) all collapse to "ruehl+veldheer". The two
    surnames are sorted (intra-pair order differs across books) and joined with
    '+', which a singles name can never produce — so a doubles key can never
    collide with a singles surname (poisoning-safe). Returns None if `name` is
    not a clean 2-player pair (so callers fall back to normalize_team)."""
    parts = [p for p in re.split(r"\s*/\s*", name.strip()) if p.strip()]
    if len(parts) != 2:
        return None
    s1 = _extract_surname(parts[0])
    s2 = _extract_surname(parts[1])
    if not s1 or not s2:
        return None
    a, b = sorted((s1, s2))
    return f"{a}+{b}"


def normalize_game(game: str) -> str:
    """
    Normalize game name to canonical form.
    
    This is THE canonical game normalization function.
    
    Handles various formats: "Counter-Strike", "counter-strike", "cs2", "cs",
    "call-of-duty", "call of duty", "mobile-legends", "mobile legends", etc.
    """
    game = game.lower().strip()
    
    # Replace hyphens with spaces for consistent matching
    game_normalized = game.replace("-", " ")
    
    # SPORTS FIRST — must be checked before "league" (which would match LoL).
    # Game names like "Cricket South Africa T20 League" contain "league" but
    # are cricket, not League of Legends.
    if "rugby" in game_normalized or game_normalized == "rby":
        return "rugby"
    elif ("cricket" in game_normalized or game_normalized == "crk"
          or "csa" in game_normalized  # Cricket South Africa
          or "t20" in game_normalized  # T20 cricket format
          or "ipl" in game_normalized  # Indian Premier League
          or "big bash" in game_normalized
          or "odi" in game_normalized
          or "test match" in game_normalized
          or "sheffield shield" in game_normalized
          or "wpl" == game_normalized  # Women's Premier League
          ):
        return "cricket"
    elif "football" in game_normalized or game_normalized == "ftb":
        return "football"
    elif "basketball" in game_normalized or game_normalized in ("nba", "nbl", "ncaab"):
        return "basketball"
    elif game_normalized in ("hockey", "ice hockey", "icehockey", "nhl", "ahl") or game_normalized.startswith("icehockey"):
        return "hockey"
    elif game_normalized in ("ufc", "mma", "mixed martial arts"):
        return "ufc"
    # ESPORTS — "league" check is safe now that sport-specific terms are handled.
    elif "counter" in game_normalized or "cs2" in game_normalized or game_normalized == "cs":
        return "cs2"
    elif "dota" in game_normalized:
        return "dota2"
    elif "league" in game_normalized or "lol" in game_normalized:
        return "lol"
    elif "valorant" in game_normalized:
        return "valorant"
    elif "mobile legends" in game_normalized or "mlbb" in game_normalized:
        return "mlbb"
    elif "call of duty" in game_normalized or "cod" in game_normalized:
        return "cod"
    elif "honor of kings" in game_normalized or "hok" in game_normalized:
        return "hok"
    elif "rainbow" in game_normalized or "r6" in game_normalized:
        return "r6"
    elif "starcraft" in game_normalized or "sc2" in game_normalized:
        return "sc2"
    
    return game


def make_match_id(team1: str, team2: str, game: str) -> str:
    """
    Create canonical match ID from teams and game.
    
    This is THE canonical match_id generation function. Do not reimplement elsewhere.
    
    Format: "{normalized_game}:{sorted_team1}:vs:{sorted_team2}"
    
    Teams are sorted alphabetically to ensure consistent IDs regardless of
    which team is listed first.
    """
    g = normalize_game(game)

    t1 = _side_key(team1, g)
    t2 = _side_key(team2, g)

    # Sort alphabetically for consistency
    if t1 > t2:
        t1, t2 = t2, t1

    return f"{g}:{t1}:vs:{t2}"


def _side_key(name: str, normalized_game: str) -> str:
    """Canonical key for ONE side. Tennis DOUBLES pairs (a name containing '/')
    get the order-/format-independent surname-set key so every book + Polymarket
    agree; everything else uses normalize_team unchanged."""
    if normalized_game == "tennis" and "/" in name:
        cp = canonical_pair(name)
        if cp is not None:
            return cp
    return normalize_team(name)


def is_individual_game_market(match_question: str) -> bool:
    """
    Check if a market is for an individual game/round within a series.
    
    These markets (e.g., "Game 1 Winner", "Round 2 Winner", "Map 3 Winner")
    are for specific games within a best-of series, NOT the overall match winner.
    We don't have odds for these, so we should skip them.
    
    Returns:
        True if this is an individual game/round market that should be skipped.
    """
    if not match_question:
        return False
    
    import re
    # Match patterns like "- Game 1 Winner", "- Round 2", "- Map 3 Winner"
    # Also match standalone titles like "Game 1 Winner" without hyphen prefix
    individual_patterns = [
        r'-\s*Game\s+\d+',       # "- Game 1 Winner"
        r'-\s*Round\s+\d+',      # "- Round 1"
        r'-\s*Map\s+\d+',        # "- Map 2 Winner"
        r'-\s*Set\s+\d+',        # "- Set 1"
        r'^Game\s+\d+\s+Winner', # "Game 1 Winner" (standalone title)
        r'^Map\s+\d+\s+Winner',  # "Map 1 Winner" (standalone title)
        r'^Round\s+\d+\s+Winner',# "Round 1 Winner" (standalone title)
        r'-\s*Who\s+wins\s+the\s+toss',  # "- Who wins the toss?" (cricket sub-market)
    ]
    
    for pattern in individual_patterns:
        if re.search(pattern, match_question, re.IGNORECASE):
            return True
    
    return False


def is_will_win_market(q_lower: str) -> bool:
    """
    Check if a question matches the 'Will X win?' or 'Will X win on DATE?' pattern.
    
    Handles:
    - "Will Harlequins win?" (rugby)
    - "Will SE Palmeiras win on 2026-03-12?" (football/soccer with date)
    - "Will National Bank of Egypt Club win on 2026-02-25?" (with date)
    """
    if not q_lower.startswith("will "):
        return False
    # Check for " win?" (direct) or " win on " (with date) or " win " at end
    return " win?" in q_lower or " win on " in q_lower


def extract_will_win_team(question: str) -> str:
    """
    Extract team name from 'Will X win?' or 'Will X win on DATE?' questions.
    
    Returns the team name (e.g., "Harlequins", "SE Palmeiras"), or empty string.
    """
    q_lower = question.lower()
    if not is_will_win_market(q_lower):
        return ""
    
    # Find the " win" keyword position (covers both "win?" and "win on DATE?")
    win_idx = q_lower.find(" win on ")
    if win_idx < 0:
        win_idx = q_lower.find(" win?")
    if win_idx < 0:
        win_idx = q_lower.rfind(" win")
    if win_idx < 0:
        return ""
    
    # team is everything between "Will " (5 chars) and " win"
    return question[5:win_idx].strip()


def parse_teams_from_question(question: str) -> tuple[str, str, str]:
    """
    Parse a Polymarket market question and return (game, team1, team2).
    
    This is a general-purpose extractor that handles:
    - "Game: Team A vs Team B (BO3)" -> ("game", "Team A", "Team B")
    - "Team A vs Team B" -> ("", "Team A", "Team B")
    - "Will Exeter Chiefs win?" -> ("rugby", "Exeter Chiefs", "")
    
    Returns: (game, team1, team2) - empty strings for missing parts.
    
    For rugby "Will X win?" markets, team2 is empty because only one team
    is known from the question. Use OddsService.get_match_by_single_team()
    to find the full match.
    """
    if not question:
        return "", "", ""
    
    q_lower = question.lower()
    
    # Pattern 1: Rugby/football "Will X win?" or "Will X win on DATE?" format
    if is_will_win_market(q_lower):
        team_name = extract_will_win_team(question)
        if team_name:
            return "rugby", team_name, ""
    
    # Pattern 2: Standard "vs" / "vs." format - clean up prefixes and suffixes
    if " vs " in question or " vs. " in question:
        clean_q = question
        game = ""
        
        # Extract game prefix if present
        if ": " in clean_q:
            parts = clean_q.split(": ", 1)
            game = parts[0].lower()
            clean_q = parts[1]
        
        # Remove (BOx) suffix if present
        if " (" in clean_q:
            clean_q = clean_q.split(" (")[0]
        
        # Split by " vs " or " vs. "
        import re
        parts = re.split(r'\s+vs\.?\s+', clean_q, maxsplit=1)
        if len(parts) == 2:
            return game, parts[0].strip(), parts[1].strip()
    
    # Pattern 3: NCAAB Spread format: "Spread: Team Name (-2.5)"
    # Returns single team — opponent unknown from question alone
    import re
    spread_match = re.match(r'^Spread:\s*(.+?)\s*\([-+]?\d+\.?\d*\)$', question)
    if spread_match:
        return "basketball", spread_match.group(1).strip(), ""
    
    # Pattern 4: NCAAB O/U format: "Over" or "Under" (no team info)
    # These are binary outcome tokens — no team can be extracted
    if q_lower.strip() in ("over", "under"):
        return "basketball", "", ""
    
    return "", "", ""


def sport_from_slug(slug: str) -> str:
    """
    Infer sport type from a Polymarket event slug.
    
    Uses keyword matching AND abbreviated prefix matching (Polymarket uses
    short coded prefixes like 'epl-', 'sea-', 'fl1-' for football leagues).
    Returns normalized game name (e.g., "rugby", "football", "cricket").
    Returns empty string if sport can't be determined.
    """
    if not slug:
        return ""
    s = slug.lower()
    if "rugby" in s or "premiership" in s:
        return "rugby"
    
    # Football: full keyword match (substrings anywhere in slug)
    if any(kw in s for kw in [
        "epl-", "la-liga", "serie-a", "bundesliga", "ligue-1", "champions-league",
        "europa-league", "conference-league", "mls-", "eredivisie", "liga-mx",
        "copa-libertadores", "copa-sudamericana", "super-lig", "primeira-liga",
        "brasileirao", "j-league", "saudi-pro", "a-league", "fa-cup", "dfb-pokal",
        "copa-del-rey", "coupe-de-france", "efl-", "concacaf", "conmebol",
        "africa-cup", "k-league", "indian-super-league",
        "soccer", "football", "liga-betplay", "primera-division",
        "copa-colombia", "liga-dimayor",
        "serie-b", "liga-1", "eliteserien", "superliga",
        "chinese-super", "j2-league", "russian-premier",
        "leagues-cup", "fifa", "caf-", "afc-", "ofc-",
        "uefa", "spanish-super-cup"
    ]):
        return "football"
    
    # Football: abbreviated Polymarket slug prefixes (e.g., "epl-not-lee-2025-11-09")
    # Collected from Polymarket URLs: polymarket.com/sports/{slug}/games
    _FOOTBALL_SLUG_PREFIXES = (
        # Major European leagues
        "epl-", "sea-", "lal-", "bun-", "fl1-", "ere-", "ucl-", "uel-",
        "spl-", "tur-", "por-", "elc-", "itc-",
        # Americas
        "mls-", "mex-", "bra-", "arg-", "col1-", "col-", "ucol-",
        "chi1-", "chi-", "per1-", "sud-", "lib-",
        # Other
        "aus-", "cde-", "cdr-", "cze1-", "den-", "nor-", "kor-",
        "egy1-", "mar1-", "rou1-", "csl-", "ja2-", "jap-", "rus-",
        "ukr1-", "isp-",
        # J-League, Bundesliga 2, Ligue 2, Serie B, Scottish Cup, Italian Cup
        "j1-", "bl2-", "fr2-", "itsb-", "scop-", "architc-", "archhhhhitc-",
        # Draft/misc
        "draft-",
        # Continental/international
        "uef-", "fif-", "caf-", "afc-", "acn-", "con-", "uwcl-",
        # Cups & misc
        "cof-", "dfb-", "ssc-", "arch-", "arch2-", "carabao-",
        # Full-word prefixes (URL-style: laliga, bundesliga, etc.)
        "laliga", "bundesliga", "ligue-", "fifa-",
    )
    if any(s.startswith(p) for p in _FOOTBALL_SLUG_PREFIXES):
        return "football"
    
    if any(kw in s for kw in ["cricket", "ipl", "t20", "odi", "big-bash", "test-match",
                               "sheffield-shield", "wpl", "wncl", "csa"]):
        return "cricket"
    # Cricket: abbreviated Polymarket slug prefixes
    _CRICKET_SLUG_PREFIXES = (
        "crint-", "criccsat20w-", "criccsa-", "cricipl-", "cricbbl-", "cricpsl-",
        "crict20blast-", "criccpl-", "cricbpl-", "cricsm-", "crict20lpl-",
        "cricsa20-", "cricilt20-", "crict20plw-", "cricmlc-", "cricwncl-",
        # Big Bash and Test Match abbreviated prefixes
        "abb-", "test-",
    )
    if any(s.startswith(p) for p in _CRICKET_SLUG_PREFIXES):
        return "cricket"
    if any(kw in s for kw in ["ncaab", "ncaa", "basketball", "nba", "cwbb",
                               "pro-a", "lnb", "kbl", "cba", "nbl", "liga-endesa"]):
        return "basketball"
    # Basketball: abbreviated Polymarket slug prefixes
    _BASKETBALL_SLUG_PREFIXES = (
        "nba-", "cbb-", "bkarg-", "bknbl-", "bkfr1-", "euroleague",
        "bkcl-", "bkligend-", "bkseriea-", "bkcba-", "bkkbl-",
        # Archived college basketball
        "archcbb-", "arch2cbb-", "archivedarch2cbb-", "archivedcbb-",
    )
    if any(s.startswith(p) for p in _BASKETBALL_SLUG_PREFIXES):
        return "basketball"
    if any(kw in s for kw in ["nhl", "ahl", "khl", "shl-", "extraliga", "del-", "hockey"]):
        return "hockey"
    # Hockey: abbreviated Polymarket slug prefixes
    _HOCKEY_SLUG_PREFIXES = (
        "nhl-", "dehl-", "khl-", "ahl-", "cehl-", "shl-", "snhl-",
    )
    if any(s.startswith(p) for p in _HOCKEY_SLUG_PREFIXES):
        return "hockey"
    if any(kw in s for kw in ["atp", "wta", "tennis"]):
        return "tennis"
    if any(kw in s for kw in ["ufc", "zuffa", "mma", "contender"]):
        return "ufc"
    # MMA: abbreviated Polymarket slug prefixes
    _MMA_SLUG_PREFIXES = (
        "dana-",
    )
    if any(s.startswith(p) for p in _MMA_SLUG_PREFIXES):
        return "ufc"
    # Rugby: abbreviated Polymarket slug prefixes
    _RUGBY_SLUG_PREFIXES = (
        "rusrp-", "ruprem-", "rusixnat-", "rutopft-", "ruurc-",
        "rueuchamp-", "ruchamp-",
    )
    if any(s.startswith(p) for p in _RUGBY_SLUG_PREFIXES):
        return "rugby"
    
    # FALLBACK: Slugs containing "vs" with team names are likely football
    # (Polymarket uses plain "team-a-vs-team-b" slugs for many football matches)
    if "-vs-" in s and "-more-markets" not in s:
        return "football"
    if s.endswith("-more-markets") and "-vs-" not in s:
        # "More Markets" variants use same slug base — infer football if no better match
        return "football"
    return ""


def parse_match_question(match_question: str, odds_service=None, event_slug: str = "") -> tuple[str, str, str]:
    """
    Parse a Polymarket match question to extract game, team1, team2.
    
    This is THE canonical function for parsing match questions.
    
    Handles various formats:
    - "Game: Team1 vs Team2 (BO1)" - standard esports format
    - "Game: Team1 vs Team2" - fallback (no suffix)
    - "Team1 vs Team2" - no game prefix
    - "Will X win?" - rugby/football/cricket binary market
    
    Args:
        match_question: The market question to parse
        odds_service: Optional OddsService to look up opponent team
        event_slug: Optional Polymarket event slug for sport classification
    
    NOTE: "Game X Winner" markets (individual games in a series) are NOT parsed.
    Use is_individual_game_market() to filter those out first.
    
    Returns:
        Tuple of (game, team1, team2). Empty strings if parsing fails.
        For "Will X win?" format, returns (sport, team_name, "").
        If odds_service provided, tries to look up both teams.
    """
    import re
    
    if not match_question:
        return "", "", ""
    
    # ===== RUGBY/FOOTBALL/CRICKET PATTERN: "Will X win?" =====
    # Extract team name and optionally look up opponent
    q_lower = match_question.lower()
    if is_will_win_market(q_lower):
        # Extract team name: "Will Harlequins win?" → "Harlequins"
        #                     "Will SE Palmeiras win on 2026-03-12?" → "SE Palmeiras"
        team_name = extract_will_win_team(match_question)
        if team_name:
            # Determine sport from event slug (primary) or leave empty for caller to infer
            sport = sport_from_slug(event_slug) or ""
            
            # CRITICAL: Strip " No" suffix for lookup - No tokens have team names like "Exeter Chiefs No"
            # but we need to find the match using the base team name "Exeter Chiefs"
            lookup_team = team_name.removesuffix(" No") if team_name.endswith(" No") else team_name
            
            # If odds_service provided, try to get both teams
            if odds_service:
                try:
                    odds_match = odds_service.get_match_by_single_team(lookup_team, sport)
                    if odds_match:
                        # Refine sport from odds match if available
                        game_type = normalize_game(odds_match.game) if odds_match.game else sport
                        return game_type, odds_match.team1, odds_match.team2
                except Exception as e:
                    import logging
                    logging.warning(f"Failed to get match for {lookup_team}: {e}")
                    pass  # Fall through to return single team
            
            # Return single team if lookup failed or no odds_service
            return sport, team_name, ""
    
    # ===== SPORTS SPREAD PATTERN: "Spread: Team Name (-2.5)" =====
    # Sports spread markets — team name extractable but no opponent
    spread_match = re.match(r'^Spread:\s*(.+?)\s*\([-+]?\d+\.?\d*\)$', match_question)
    if spread_match:
        team_name = spread_match.group(1).strip()
        spread_sport = sport_from_slug(event_slug) if event_slug else ""
        return spread_sport or "unknown", team_name, ""
    
    # ===== SPORTS O/U PATTERN: question is just "Over" or "Under" =====
    if q_lower.strip() in ("over", "under"):
        ou_sport = sport_from_slug(event_slug) if event_slug else ""
        return ou_sport or "unknown", "", ""
    
    # ===== TENNIS SUB-MARKET PATTERNS =====
    # Tennis events have sub-markets with distinctive question formats:
    #   - "Set Handicap: Baena (-1.5)" — spread-style with handicap
    #   - "Set 1 Winner: Busta vs Harris" — set winner with vs
    #   - "Total Sets O/U 2.5" — total sets over/under
    #   - "Set 1 Games O/U 9.5" — set games over/under
    #   - "Match O/U 23.5" — match total over/under
    #   - "Over 2.5" / "Under 2.5" — O/U outcome tokens
    
    # Set Handicap: "Set Handicap: Baena (-1.5)" or "Set Handicap: Auger-Aliassime (-1.5) vs Zhang (+1.5)"
    if q_lower.startswith("set handicap:"):
        # Try two-player format first: "Player1 (-1.5) vs Player2 (+1.5)"
        two_player = re.match(
            r'^Set Handicap:\s*(.+?)\s*\([-+]?\d+\.?\d*\)\s+vs\.?\s+(.+?)\s*\([-+]?\d+\.?\d*\)$',
            match_question, re.IGNORECASE
        )
        if two_player:
            return "tennis", two_player.group(1).strip(), two_player.group(2).strip()
        # Fallback: single-player format "Player (-1.5)"
        team_match = re.match(r'^Set Handicap:\s*(.+?)\s*\([-+]?\d+\.?\d*\)$', match_question, re.IGNORECASE)
        team_name = team_match.group(1).strip() if team_match else ""
        return "tennis", team_name, ""
    
    # Set Winner: "Set 1 Winner: Busta vs Harris"
    set_winner_match = re.match(r'^Set \d+ Winner:\s*(.+?)\s+vs\.?\s+(.+?)$', match_question, re.IGNORECASE)
    if set_winner_match:
        return "tennis", set_winner_match.group(1).strip(), set_winner_match.group(2).strip()
    
    # Total Sets / Set Games / Match O/U: "Total Sets O/U 2.5", "Set 1 Games O/U 9.5", "Match O/U 23.5"
    if re.match(r'^(?:Total Sets|Set \d+ Games|Match)\s+O/U\s+\d+\.?\d*$', match_question, re.IGNORECASE):
        return "tennis", "", ""
    
    # Over/Under with number: "Over 2.5", "Under 23.5" (tennis O/U outcome tokens)
    if re.match(r'^(?:Over|Under)\s+\d+\.?\d*$', match_question, re.IGNORECASE):
        return "tennis", "", ""
    
    # ===== WEATHER PATTERN: "Will the highest temperature in X be Y on DATE?" =====
    # Used by SpreadBot for weather temperature markets
    if "highest temperature" in q_lower or "lowest temperature" in q_lower:
        # Extract city and temperature bin from weather question
        # Pattern: "Will the highest temperature in Miami be between 66-67°F on January 28?"
        
        # Match city: everything between "temperature in " and " be " 
        # (NOT greedy - stops before "be" keyword)
        city_match = re.search(r'temperature in (.+?)\s+be\s', match_question, re.IGNORECASE)
        city = city_match.group(1).strip() if city_match else "unknown"
        
        # Normalize city names to match WeatherMarketClient conventions
        CITY_NORMALIZE = {
            "new york city": "new york",
            "nyc": "new york",
        }
        
        # Match temperature range/bin
        temp_match = re.search(r'be (?:between\s+)?([^?]+?)(?:\s+on\s+|\?)', match_question, re.IGNORECASE)
        temp_bin = temp_match.group(1).strip() if temp_match else "unknown"
        
        # Match date (e.g., "January 28")
        date_match = re.search(r'on\s+(\w+\s+\d+)', match_question, re.IGNORECASE)
        date_str = date_match.group(1).replace(" ", "-").lower() if date_match else ""
        
        # CRITICAL: Include city and date in team names for unique match_ids per city/date
        # This prevents collisions between e.g. "Seattle 50-51°F" and "Atlanta 50-51°F"
        city_lower = CITY_NORMALIZE.get(city.lower(), city.lower())
        yes_team = f"{city_lower}:{temp_bin}"
        no_team = f"{city_lower}:Not {temp_bin}"
        
        # For weather, return game="weather", team1=city:bin_label, team2=city:Not bin_label
        return "weather", yes_team, no_team
    
    # ===== MENTIONS PATTERN: "Will X say/mention Y during/by/in Z?" =====
    # Mentions markets are binary bets on whether a word/phrase will be said,
    # or whether an event will air.
    # Questions like:
    #   - 'Will Trump say "Low Energy" by February 28?'
    #   - 'Will "DOJ" be said during the first episode of the Joe Rogan Experience this week?'
    #   - 'Will Coinbase say "Margin" during earnings call?'
    #   - 'Will "Switzerland" be said during the next episode of the All-In Podcast?'
    #   - "Will Hillary's remarks not air?" (broadcast mentions)
    #   - "Will Hillary's remarks air?" (broadcast mentions)
    mentions_keywords = ("say ", "said ", "mention ", "name ", " air?", " not air?")
    if q_lower.startswith("will ") and any(kw in q_lower for kw in mentions_keywords):
        # Try to extract a quoted word/phrase (the bet outcome)
        quoted_match = re.search(r'["\u201c\u201d]([^"\u201c\u201d]+)["\u201c\u201d]', match_question)
        if quoted_match:
            word = quoted_match.group(1).strip()
            yes_team = f"mentions:{word}"
            no_team = f"mentions:Not {word}"
            return "mentions", yes_team, no_team
        
        # No quoted phrase — extract subject from "Will X verb?" pattern
        # e.g. "Will Hillary's remarks not air?" → subject = "Hillary's remarks not air"
        subject_match = re.match(r'^Will\s+(.+?)[\s?]*$', match_question, re.IGNORECASE)
        if subject_match:
            subject = subject_match.group(1).strip().rstrip("?")
            yes_team = f"mentions:{subject}"
            no_team = f"mentions:Not {subject}"
            return "mentions", yes_team, no_team
    
    # ===== STOCK PATTERN: "Tesla (TSLA) Up or Down on February 5?" =====
    # Used by SpreadBot for stock price direction markets
    if "up or down on" in q_lower:
        import re
        
        # Match ticker symbol in parentheses, e.g. "(TSLA)"
        ticker_match = re.search(r'\(([A-Z]+)\)', match_question)
        ticker = ticker_match.group(1) if ticker_match else None
        
        # Match date (e.g., "February 5")
        date_match = re.search(r'on\s+(\w+)\s+(\d+)', match_question, re.IGNORECASE)
        if date_match:
            month = date_match.group(1)[:3].lower()  # "feb"
            day = date_match.group(2)  # "5"
            date_suffix = f"-{month}{day}"  # "-feb5"
        else:
            date_suffix = ""
        
        if ticker:
            # Include date to differentiate Feb 5 from Feb 6
            # e.g., "TSLA-feb5:Up" and "TSLA-feb5:Down"
            yes_team = f"{ticker}{date_suffix}:Up"
            no_team = f"{ticker}{date_suffix}:Down"
            return "stock", yes_team, no_team
    
    # Pattern 1: Standard format with (BOx) suffix
    # "Counter-Strike: Team A vs Team B (BO3)"
    match_pattern = r"^(.+?):\s*(.+?)\s+vs\.?\s+(.+?)\s*\("
    m = re.search(match_pattern, match_question)
    
    if not m:
        # Pattern 2: With game prefix, no suffix
        # "Counter-Strike: Team A vs Team B"
        match_pattern = r"^(.+?):\s*(.+?)\s+vs\.?\s+(.+?)$"
        m = re.search(match_pattern, match_question)
    
    if not m:
        # Pattern 4: No game prefix, with (BOx) suffix
        # "Team A vs Team B (BO3)" / "Team A vs. Team B (BO3)"
        match_pattern = r"^(.+?)\s+vs\.?\s+(.+?)\s*\("
        m = re.search(match_pattern, match_question)
        if m:
            t1, t2 = _strip_cricket_suffix(m.group(1).strip()), _strip_cricket_suffix(m.group(2).strip())
            return "", t1, t2
    
    if not m:
        # Pattern 5: No game prefix, no suffix
        # "Team A vs Team B" / "Team A vs. Team B"
        match_pattern = r"^(.+?)\s+vs\.?\s+(.+?)$"
        m = re.search(match_pattern, match_question)
        if m:
            t1, t2 = _strip_cricket_suffix(m.group(1).strip()), _strip_cricket_suffix(m.group(2).strip())
            return "", t1, t2
    
    if m and len(m.groups()) >= 3:
        game = m.group(1).strip().lower()
        team1 = _strip_cricket_suffix(m.group(2).strip())
        team2 = _strip_cricket_suffix(m.group(3).strip())
        return game, team1, team2
    
    return "", "", ""


def _strip_cricket_suffix(team_name: str) -> str:
    """
    Strip cricket sub-market suffixes from team names.
    
    Cricket markets on Polymarket have sub-markets like:
    - "Pakistan - Who wins the toss?" → "Pakistan"
    - "England Lions - Team Top Batter - England Lions Winner" → "England Lions"
    - "Pakistan - Toss/Match Double - Pakistan Winner" → "Pakistan"  
    - "Pakistan - Completed Match?" → "Pakistan"
    - "Pakistan A - Most Sixes - England Lions Winner" → "Pakistan A"
    
    These suffixes get parsed as part of the team name and break team matching.
    """
    import re
    # Strip everything after " - " that looks like a cricket sub-market suffix
    # Common patterns: "- Who wins the toss?", "- Team Top Batter...", "- Most Sixes...",
    # "- Toss/Match Double...", "- Completed Match?"
    cricket_suffixes = [
        r'\s*-\s*Who\s+wins\s+the\s+toss\??',
        r'\s*-\s*Team\s+Top\s+Batter.*',
        r'\s*-\s*Most\s+Sixes.*',
        r'\s*-\s*Toss[/\s]Match\s+Double.*',
        r'\s*-\s*Completed\s+Match\??',
    ]
    for pattern in cricket_suffixes:
        team_name = re.sub(pattern, '', team_name, flags=re.IGNORECASE).strip()
    return team_name
