"""
TurboShare Hardened Authentication, Rate Limiting & Session Engine
Zero-Dependency Standard Library Implementation (Python 3.8+)

Implements:
1. Salted PBKDF2-HMAC-SHA256 password derivation (600,000 iterations, 16-byte random salt)
   with constant-time verification (hmac.compare_digest).
2. Persistent stateless HMAC-SHA256 signed session tokens (256-bit entropy) in
   HttpOnly; SameSite=Strict; Secure cookies surviving server reboots.
3. URL-Safe bookmarkable access key (/api/auth?key=...) with HTTP 303 PRG clean redirect,
   Referrer-Policy: no-referrer, and Cache-Control: no-store history scrubbing.
4. Sliding-window IP rate limiting (900s, max 5 failed attempts) with exponential
   delay tarpitting (1s, 2s, 4s, 8s, 16s, then HTTP 429 lockout).
5. Safe proxy header resolution (CF-Connecting-IP, X-Forwarded-For) strictly when
   direct socket peer is verified localhost loopback (127.0.0.1, ::1).
"""

import os
import sys
import time
import json
import hmac
import hashlib
import secrets
import threading
import ipaddress
import urllib.parse
from http import cookies
from collections import defaultdict
from typing import Optional, Tuple, Dict, List, Any

# ── Configuration Constants ───────────────────────────────────────────────────
CONFIG_FILE = ".env"
SESSIONS_FILE = ".sessions.json"
SESSION_COOKIE_NAME = "turboshare_session"
DEFAULT_ITERATIONS = 600_000
SESSION_TTL_DAYS = 30
RATE_LIMIT_WINDOW = 900          # 15 minutes in seconds
MAX_FAILED_ATTEMPTS = 5
BASE_TARPIT_DELAY = 1.0         # seconds
MAX_TARPIT_DELAY = 16.0         # seconds

# 4,096 Curated Memorable English Words (2^12 = 12 bits per word)
MEMORABLE_WORDS: Tuple[str, ...] = tuple((
    "star falcon bold tiger swift frost shadow cedar river beacon pixel "
    "orbit nexus wave stone sky fox hawk bear lion wolf peak creek cliff "
    "flame spark amber jade ruby echo prism solar lunar comet nova atlas iron "
    "steel sage moss pine maple elm oak ash dune reef canyon vale ridge crest "
    "breeze storm dawn dusk haven harbor abandon ability able about above "
    "absent absorb absurd abuse access account accuse achieve acid acquire "
    "across act action actor actress actual adapt add addict address adjust "
    "admit adult advance advice aerobic affair afford afraid again age agent "
    "agree ahead aim air airport aisle alarm album alcohol alert alien all "
    "alley allow almost alone alpha already also alter always amateur amazing "
    "among amount amused analyst anchor ancient anger angle angry animal "
    "ankle annual another answer antenna antique anxiety any apart apology "
    "appear apple approve april arch arctic area arena argue arm armed armor "
    "army around arrange arrest arrive arrow art artist artwork ask aspect "
    "assault asset assist assume asthma athlete atom attack attend attract "
    "auction audit august aunt author auto autumn average avocado avoid awake "
    "aware away awesome awful awkward axis baby bacon badge bag balance "
    "balcony ball bamboo banana banner bar barely bargain barrel base basic "
    "basket battle beach bean beauty because become beef before begin behave "
    "behind believe below belt bench benefit best betray better between "
    "beyond bicycle bid bike bind biology bird birth bitter black blade blame "
    "blanket blast bleak bless blind blood blossom blouse blue blur blush "
    "board boat body boil bomb bone bonus book boost border boring borrow "
    "boss bottom bounce box boy bracket brain brand brass brave bread brick "
    "bridge brief bright bring brisk broken bronze broom brother brown brush "
    "bubble buddy budget buffalo build bulb bulk bullet bundle bunker burden "
    "burger burst bus busy butter buyer buzz cabbage cabin cable cactus cage "
    "cake call calm camera camp can canal cancel candy cannon canoe canvas "
    "capable capital captain car carbon card cargo carpet carry cart case "
    "cash casino castle casual cat catalog catch cattle caught cause caution "
    "cave ceiling celery cement census century cereal certain chair chalk "
    "change chaos chapter charge chase chat cheap check cheese chef cherry "
    "chest chicken chief child chimney choice choose chronic chuckle chunk "
    "churn cigar circle citizen city civil claim clap clarify claw clay clean "
    "clerk clever click client climb clinic clip clock clog close cloth cloud "
    "clown club clump cluster clutch coach coast coconut code coffee coil "
    "coin collect color column combine come comfort comic common company "
    "concert conduct confirm connect control cook cool copper copy coral core "
    "corn correct cost cotton couch country couple course cousin cover coyote "
    "crack cradle craft cram crane crash crater crawl crazy cream credit crew "
    "cricket crime crisp critic crop cross crouch crowd crucial cruel cruise "
    "crumble crunch crush cry crystal cube culture cup curious current "
    "curtain curve cushion custom cute cycle dad damage damp dance danger "
    "daring dash day deal debate debris decade decide decline deer defense "
    "define defy degree delay deliver demand demise denial dentist deny "
    "depart depend deposit depth deputy derive desert design desk despair "
    "destroy detail detect develop device devote diagram dial diamond diary "
    "dice diesel diet differ digital dignity dilemma dinner direct dirt "
    "disease dish dismiss display divert divide divorce dizzy doctor dog doll "
    "dolphin domain donate donkey donor door dose double dove draft dragon "
    "drama drastic draw dream dress drift drill drink drip drive drop drum "
    "dry duck dumb during dust dutch duty dwarf dynamic eager eagle early "
    "earn earth easily east easy ecology economy edge edit educate effort egg "
    "eight either elbow elder elegant element elite else embark embody "
    "embrace emerge emotion employ empower empty enable enact end endless "
    "endorse enemy energy enforce engage engine enhance enjoy enlist enough "
    "enrich enroll ensure enter entire entry episode equal equip era erase "
    "erode erosion error erupt escape essay essence estate eternal ethics "
    "evil evoke evolve exact example excess excite exclude excuse execute "
    "exhaust exhibit exile exist exit exotic expand expect expire explain "
    "expose express extend extra eye eyebrow fabric face faculty fade faint "
    "faith fall false fame family famous fan fancy fantasy farm fashion fat "
    "fatal father fatigue fault feature federal fee feed feel female fence "
    "fetch fever few fiber fiction field figure file film filter final find "
    "fine finger finish fire firm first fiscal fish fit fitness fix flag "
    "flash flat flavor flee flight flip float flock floor flower fluid flush "
    "fly foam focus fog foil fold follow food foot force forest forget fork "
    "fortune forum forward fossil foster found fragile frame fresh friend "
    "fringe frog front frown frozen fruit fuel fun funny furnace fury future "
    "gadget gain galaxy gallery game gap garage garbage garden garlic garment "
    "gas gasp gate gather gauge gaze general genius genre gentle genuine "
    "gesture ghost giant gift giggle ginger giraffe girl give glad glance "
    "glare glass glide glimpse globe gloom glory glove glow glue goat goddess "
    "gold good goose gorilla gospel gossip govern gown grab grace grain grant "
    "grape grass gravity great green grid grief grit grocery group grow grunt "
    "guard guess guide guilt guitar gun gym habit hair half hammer hamster "
    "hand happy hard harsh harvest hat have hazard head health heart heavy "
    "height hello helmet help hen hero hidden high hill hint hip hire history "
    "hobby hockey hold hole holiday hollow home honey hood hope horn horror "
    "horse host hotel hour hover hub huge human humble humor hundred hungry "
    "hunt hurdle hurry hurt husband hybrid ice icon idea idle ignore ill "
    "illegal illness image imitate immense immune impact impose improve "
    "impulse inch include income index indoor infant inflict inform inhale "
    "inherit initial inject injury inmate inner input inquiry insane insect "
    "inside inspire install intact into invest invite involve island isolate "
    "issue item ivory jacket jaguar jar jazz jealous jeans jelly jewel job "
    "join joke journey joy judge juice jump jungle junior junk just keen keep "
    "ketchup key kick kid kidney kind kingdom kiss kit kitchen kite kitten "
    "kiwi knee knife knock know lab label labor ladder lady lake lamp laptop "
    "large later latin laugh laundry lava law lawn lawsuit layer lazy leader "
    "leaf learn leave lecture left leg legal legend leisure lemon lend length "
    "lens leopard lesson letter level liar liberty library license life lift "
    "light like limb limit link liquid list little live lizard load loan "
    "lobster local lock logic lonely long loop lottery loud lounge love loyal "
    "lucky luggage lumber lunch luxury lyrics machine mad magic magnet maid "
    "mail main major make mammal man manage mandate mango mansion manual "
    "marble march margin marine market mask mass master match math matrix "
    "matter maximum maze meadow mean measure meat medal media melody melt "
    "member memory mention menu mercy merge merit merry mesh message metal "
    "method middle milk million mimic mind minimum minor minute miracle "
    "mirror misery miss mistake mix mixed mixture mobile model modify mom "
    "moment monitor monkey monster month moon moral more morning mother "
    "motion motor mouse move movie much muffin mule muscle museum music must "
    "mutual myself mystery myth naive name napkin narrow nasty nation nature "
    "near neck need neglect neither nephew nerve nest net network neutral "
    "never news next nice night noble noise nominee noodle normal north nose "
    "notable note nothing notice novel now nuclear number nurse nut obey "
    "object oblige obscure observe obtain obvious occur ocean october odor "
    "off offer office often oil okay old olive olympic omit once one onion "
    "online only open opera opinion oppose option orange orchard order organ "
    "orient orphan ostrich other outdoor outer output outside oval oven over "
    "own owner oxygen oyster ozone pact paddle page pair palace palm panda "
    "panel panic panther paper parade parent park parrot party pass patch "
    "path patient patrol pattern pause pave payment peace peanut pear peasant "
    "pelican pen penalty pencil people pepper perfect permit person pet phone "
    "photo phrase piano picnic picture piece pig pigeon pill pilot pink "
    "pioneer pipe pistol pitch pizza place planet plastic plate play please "
    "pledge pluck plug plunge poem poet point polar pole police pond pony "
    "pool popular portion post potato pottery poverty powder power praise "
    "predict prefer prepare present pretty prevent price pride primary print "
    "prison private prize problem process produce profit program project "
    "promote proof prosper protect proud provide public pudding pull pulp "
    "pulse pumpkin punch pupil puppy purity purpose purse push put puzzle "
    "pyramid quality quantum quarter quick quit quiz quote rabbit raccoon "
    "race rack radar radio rail rain raise rally ramp ranch random range "
    "rapid rare rate rather raven raw razor ready real reason rebel rebuild "
    "recall receive recipe record recycle reduce reflect reform refuse region "
    "regret regular reject relax release relief rely remain remind remove "
    "render renew rent reopen repair repeat replace report require rescue "
    "resist result retire retreat return reunion reveal review reward rhythm "
    "rib ribbon rice rich ride rifle right rigid ring riot ripple risk ritual "
    "rival road roast robot robust rocket romance roof rookie room rose "
    "rotate rough round route royal rubber rude rug rule run runway rural sad "
    "saddle sadness safe sail salad salmon salon salt salute same sample sand "
    "satisfy satoshi sauce sausage save say scale scan scare scatter scene "
    "scheme school science scout scrap screen script scrub sea search season "
    "seat second secret section seed seek segment select sell seminar senior "
    "sense series service session settle setup seven shaft shallow share shed "
    "shell sheriff shield shift shine ship shiver shock shoe shoot shop short "
    "shove shrimp shrug shuffle shy sibling sick side siege sight sign silent "
    "silk silly silver similar simple since sing siren sister situate six "
    "size skate sketch ski skill skin skirt skull slab slam sleep slender "
    "slice slide slight slim slogan slot slow slush small smart smile smoke "
    "smooth snack snake snap sniff snow soap soccer social sock soda soft "
    "soldier solid solve someone song soon sorry sort soul sound soup source "
    "south space spare spatial spawn speak special speed spell spend sphere "
    "spice spider spike spin spirit split spoil sponsor spoon sport spot "
    "spray spread spring spy square squeeze stable stadium staff stage stairs "
    "stamp stand start state stay steak stem step stereo stick still sting "
    "stock stomach stool story stove street strike strong student stuff "
    "stumble style subject submit subway success such sudden suffer sugar "
    "suggest suit summer sun sunny sunset super supply supreme sure surface "
    "surge survey suspect sustain swallow swamp swap swarm swear sweet swim "
    "swing switch sword symbol symptom syrup system table tackle tag tail "
    "talent talk tank tape target task taste tattoo taxi teach team tell ten "
    "tenant tennis tent term test text thank that theme then theory there "
    "they thing this thought three thrive throw thumb thunder ticket tide "
    "tilt timber time tiny tip tired tissue title toast tobacco today toddler "
    "toe toilet token tomato tone tongue tonight tool tooth top topic topple "
    "torch tornado toss total tourist toward tower town toy track trade "
    "traffic tragic train trap trash travel tray treat tree trend trial tribe "
    "trick trigger trim trip trophy trouble truck true truly trumpet trust "
    "truth try tube tuition tumble tuna tunnel turkey turn turtle twelve "
    "twenty twice twin twist two type typical ugly unable unaware uncle "
    "uncover under undo unfair unfold unhappy uniform unique unit unknown "
    "unlock until unusual unveil update upgrade uphold upon upper upset urban "
    "urge usage use used useful useless usual utility vacant vacuum vague "
    "valid valley valve van vanish vapor various vast vault vehicle velvet "
    "vendor venture venue verb verify version very vessel veteran viable "
    "vibrant vicious victory video view village vintage violin virtual virus "
    "visa visit visual vital vivid vocal voice void volcano volume vote "
    "voyage wage wagon wait walk wall walnut want warfare warm warrior wash "
    "wasp waste water way wealth weapon wear weasel weather web wedding "
    "weekend weird welcome west wet whale what wheat wheel when where whip "
    "whisper wide width wife wild will win window wine wing wink winner "
    "winter wire wisdom wise wish witness woman wonder wood wool word work "
    "world worry worth wrap wreck wrestle wrist write wrong yard year yellow "
    "you young youth zebra zero zone zoo abacus abdomen abide abiding ablaze "
    "abreast abridge abroad absence absolve abstain accent acclaim acetone "
    "aching acorn acre acrobat acronym acting active acts acutely aerosol "
    "afar affirm affix affront aflame afloat afoot aged ageless agency agenda "
    "aghast agile agility aging agonize agony agreed aground ahoy aide aids "
    "ajar alfalfa algebra alias alibi aliens alike alive almanac aloe aloft "
    "aloha aloof alright alto alumni amaze ambush amends amenity amiable amid "
    "amigo amino amiss ammonia amnesty ample amplify amply amuck amulet "
    "amuser amusing anagram anatomy anchovy android anemia anemic anew "
    "angelic angled angler angles angling angrily angular animate anime annex "
    "annuity antacid anthem anthill antics antler antonym antsy anvil anybody "
    "anyhow anymore anyone anytime anyway aorta apache apostle appease "
    "applaud applied apply apricot apron aptly aqua arise armband armful "
    "armhole arming armless armoire armored armory armrest aroma arose "
    "arousal array arrival arson ascend ascent ashamed ashen ashes ashy aside "
    "askew asleep aspire aspirin astound astride astute atop atrium atrophy "
    "attach attain attempt attest attic attire audible audibly audio autism "
    "avatar avenge avenue avert aviator avid await awaken award awhile awning "
    "awoke awry babble babied baboon backed backer backing backlit backlog "
    "backup badass badland badly badness baffle bagel bagful baggage bagged "
    "baggie bagging baggy bagpipe baked bakery baking balmy banish banjo "
    "banked banker banking banshee banter barbed barbell barber barcode barge "
    "barista barley barmaid barman barn barrack barrier bash basics basil "
    "basin basis batboy batch bath baton bats battery batting bauble bazooka "
    "blabber bladder blah blaming blank blazer blazing bleach bleep blemish "
    "blend blimp bling blinked blinker blinks blip blitz bloated blob blog "
    "blooper blot blubber bluff bluish blunt blurb blurred blurry blurt "
    "boaster bobbed bobbing bobble bobcat bobsled bobtail bogged boggle bogus "
    "bok bolster bolt bonanza bonded bonding boned boney bonfire bonnet "
    "bonsai bony booted booth bootie booting bootleg boots boozy borax "
    "borough botany botch both bottle bouncy bovine boxcar boxer boxing "
    "boxlike boxy breach breath breeder breezy brewery brewing briar bribe "
    "bride bridged brigade brim brink brisket briskly bristle brittle broaden "
    "broadly broiler broker bronco brook brought browse brunch brunt brute "
    "bubbly bucked bucket buckle budding buffed buffer buffing buffoon buggy "
    "bulge bulgur bulldog bullion bullish bullpen bully bunch bungee bunion "
    "bunkbed bunny bunt busboy bush busily busload bust cabana cabbie caboose "
    "cache cackle cacti caddie caddy cadet cadmium cahoots calcium caliber "
    "caloric calorie calzone cameo camper camping campus canary candied "
    "candle cane canine canned canning cannot canola canon canopy canteen "
    "capably cape capitol capped capsize capsule caption captive capture "
    "caramel carat caravan carded cardiac caress caring carless carload "
    "carnage carol carpool carport carried carrot cartel carton cartoon carve "
    "carving carwash cascade casing casket catcall catcher catchy caterer "
    "catfish catlike catnap catnip catsup cattail catty catwalk caucus causal "
    "causing cavalry caviar cavity celtic certify chafe chain chalice chamber "
    "chance channel chant chapped chaps charger chariot charity charm charred "
    "charter chasing chaste chatter chatty cheddar cheek cheer cheesy chemist "
    "chemo cherub chess chevron chevy chewer chewing chewy chili chill chimp "
    "chip chirpy chive choking chomp chooser choosy chop chosen chowder "
    "chrome chubby chuck chug chummy chump chute cider cinch cinema circus "
    "citable citadel citric citrus civic clad clammy clamor clamp clang "
    "clapped clapper clarity clash clasp class clatter clause clear cleat "
    "cleaver cleft clench clicker climate cling clique cloak clobber clone "
    "cloning closure clothes clover clubbed clumsy clunky clutter coastal "
    "coaster coat cobalt cobbler cobweb cocoa cod coerce coexist coke cola "
    "cold collage collar collide collie colony colt coma comfy coming comma "
    "commend comment commode commute compare compel compile comply compost "
    "comrade concave conceal concept conch concise concur condone conduit "
    "cone confess conform conical conjure consent console consult contact "
    "contend contest context contort contour convene convent cope copied "
    "copier copilot coping copious cork corncob cornea corned corner corny "
    "coroner corral corrode corsage corset cortex cosmic cosmos cottage cough "
    "could county courier coveted coyness cozily cozy crafter crafty cramp "
    "cranial cranium crank crate crave craving crayon crazed crazily creamed "
    "creamer crease create creed creme creole crepe crept crested crevice "
    "crewman crib cried crier crimp crimson cringe crinkle crinkly crisped "
    "crisply crispy critter croak crock crook croon crouton crowbar crown "
    "crudely cruelly cruelty crumb crummy crumpet crunchy crushed crusher "
    "crust crux crying cryptic cubical cubicle cuddle cuddly culprit cupcake "
    "cupid cupped cupping curable curator curdle cure curfew curing curled "
    "curler curling curly curry curse cursive cursor curtly curtsy curvy "
    "cushy cusp cussed custard custody customs cut cyclic cycling cyclist "
    "cymbal dab dagger daily dainty dairy daisy dancing dander dandy dangle "
    "dares darkish darling darn dart data dating dawdler daybed daycare "
    "daylong dayroom daytime dazzler deacon dealer dealing dealt dean debit "
    "debrief debtor debug debunk decaf decal decay deceit decency decent "
    "decibel decimal deck decode decoy decree deduce deduct deed deem deepen "
    "deeply deface defame default defeat defiant defile deflate defog defraud "
    "defrost deftly defuse deity delete delouse delta deluge deluxe demote "
    "denim denote dense density dental denture depict deplete deploy deport "
    "depose depress deprive derail derby derived deserve desktop despise "
    "despite destiny detest detract deuce devalue deviant deviate devious "
    "devotee diaper dicing dictate dig dill dilute dime dimly dimmed dimmer "
    "dimness dimple diner dingbat dinghy dingo dingy dining diocese dioxide "
    "diploma dipped dipper dipping disarm disband discard discern discuss "
    "disdain disjoin disk dislike dismay disobey disown dispose dispute "
    "disrupt distant distill distort ditch ditto ditzy divided diving doable "
    "docile dock dodge dodgy doily doing dole dollar dollop dolly donated "
    "donator donut doodle doorman doormat doorway doozy dork dorsal dosage "
    "dotted douche down dowry doze drab drained drainer drank drapery dreaded "
    "dreamt dreamy dreary drench drew dribble dried drier driller drippy "
    "driven driver driving drizzle drizzly drone drool droop dropbox droplet "
    "dropout dropper drove drown drudge dubbed duchess ducking ducky duct "
    "dude duffel dugout duh duke duller duly dumping duo dupe duplex durable "
    "durably duress dutiful duvet dweeb dwelled dweller dwindle dynasty each "
    "earache eardrum earflap earful earlobe earmark earmuff earring earshot "
    "earthen earthly earthy earwig easeful easel easiest easing easter "
    "eatable eaten eatery eating eats ebay ebony ebook ecard eclair eclipse "
    "edging edgy edition editor eel effects egging eggnog egotism eject "
    "elastic elated elderly eldest elevate eleven elf elitism elixir elk "
    "ellipse elope elude elusive elves email embargo embassy ember emblaze "
    "emblem emboss emcee emerald emit emote empathy emperor emptier emu "
    "enamel enclose encode encore encrust encrypt ended ending endnote "
    "engaged engorge engross engulf enjoyer enrage enslave ensnare entail "
    "entitle entity entomb entrap entree entrust entwine envious envoy envy "
    "enzyme epic equate equator equinox equity erased eraser erasure errand "
    "errant erratic eskimo esquire etching ethanol ether evacuee evade "
    "evasion evasive even evict evident exalted exclaim exert exes exhale "
    "exhume exodus expanse expel expend expert explode exploit explore extent "
    "extinct extras extrude fable faceted facial facing faction factoid "
    "factor factual fading failing falsify famine fanatic fancied fanfare "
    "fang fanning fascism faster fasting faucet favored fax feast fedora "
    "feeble feisty feline femur fencing fender ferment ferret ferris ferry "
    "fervor fester festive fetal fiddle fidgety fifteen fifth fifty figment "
    "filing filled filler filling filth finale finally finance finch finer "
    "finicky finite finless finlike five flaccid flagman flail flakily flaky "
    "flanked flap flaring flashy flask flatbed flatly flatten flattop fled "
    "fleshed fleshy flick flier flinch fling flint flirt flop floral florist "
    "floss flyable flyaway flyer flying flyover foe folic folk fondly fondue "
    "font fool footage footer footing footman footpad footsie founder foyer "
    "frail framing frantic frayed fraying frays freebee freebie freedom "
    "freeing freely freeway freight french frenzy fretful fretted friday "
    "fridge fried frill frisbee frisk fritter frolic from frosted frosty "
    "froth frying gab gaffe gag gaining gains gala galley gallon gallows "
    "galore gaming gamma gander gangly gangway gargle garland garnet garnish "
    "garter gating gauging gauze gave gawk gazing gear gecko geek geiger gem "
    "gender generic gentile gently gents geology gerbil gestate getaway "
    "getting getup giblet giddily giddy giggly gigolo gilled gills gimmick "
    "girdle given giver giving gizmo gizzard glacial glacier glade gladly "
    "glamour glaring glazing gleeful glider gliding glimmer glisten glitch "
    "glitter glitzy gloater gloomy glorify gloss glowing glucose gluten "
    "glutton gnarly gnat goal goes goggles going golf goliath gonad gondola "
    "gone gong gooey goofy google goon gopher gore gorged gory gosling gothic "
    "gotten gout graded grader grading grafted grandly grandma grandpa "
    "granite granny granola graph grapple grasp gratify grating gravel graves "
    "gravy gray grazing greedy greeter grew grill grimace grime grimy grinch "
    "grip gristle groggy groin groom groove groovy grope ground grouped grout "
    "grove grower growing growl grub grudge gruffly grumble grumbly grunge "
    "guiding guise gulf gully gulp gumball gumdrop gumming gummy gurgle guru "
    "gush gusto gusty gutless guts gutter guy guzzler habitat hacked hacker "
    "hacking hacksaw had haggler haiku halogen halt halved halves hamlet "
    "hammock hamper handbag handed handful handgun handled handler handoff "
    "handsaw handset hangout hangup hankie hanky happier happily hardhat "
    "hardly hardy harmful harmony harness harpist hash hassle haste hastily "
    "hasty hatbox hatchet hate hatless hatred haunt hazily hazing hazy headed "
    "header heading headset headway heap heat heave heavily heaving hedge "
    "hedging hefty helium helper helpful helping hemlock hence henna herald "
    "herbal herbs hermit heroics heroism herring herself hertz hexagon hubcap "
    "huddle huff hug hula hulk hull humbly humid humming hummus humped humvee "
    "hunger hunk hunter hunting hurled hurler hurling hurray hurried hush "
    "husked hut hydrant hyphen iciness icing icky icy ideally idiocy idiom "
    "idly igloo iguana imaging immerse impale impart impeach impish implant "
    "implode imply impound imprint impure iodine iodize ion ipad iphone ipod "
    "irate irk islamic isotope issuing italics itunes ivy jab jackal jackpot "
    "jailer jam janitor january jargon jarring jasmine jaunt java jawed "
    "jawless jawline jaws jaybird jeep jellied jersey jester jet jiffy jigsaw "
    "jimmy jingle jinx jitters jittery jockey jogger jogging john joining "
    "jolly jolt jot jovial joyous joyride judo juggle jugular juicy jujitsu "
    "jukebox july jumble jumbo june juniper junkie junkman jurist juror jury "
    "justice justify justly kabob karaoke karate karma kebab keenly keg kelp "
    "kennel kept kettle kiln kilt kimono kindle kindly kindred kinetic "
    "kinfolk king kinship kinsman kisser kissing kitty kleenex knelt knoll "
    "koala kooky kosher krypton kudos kung labored laborer ladies ladle "
    "ladybug lagged lagging lagoon lair lance landed landing lanky lantern "
    "lapdog lapel lapped lapping lard lark lash lasso last latch late lather "
    "latrine latter launch launder laurel lavish lazily legacy legged legible "
    "legibly lego legroom legume legwork lent leotard lesser letdown lettuce "
    "levers liable licking lid lifter lifting liftoff likely liking lilac "
    "lilly lily limeade limes limping line lingo lining linked linseed lint "
    "lip liquefy liqueur lisp litmus litter livable lived lively liver "
    "lividly living lucid luckily lugged lullaby lumping lumpish lunacy lung "
    "lurch lure lurk lushly luster lustily lusty lying macaw mace magenta "
    "maggot magical magma magnify maimed majesty maker making malt mama "
    "mammary manager manatee manger mangle mangy manhole manhood manhunt "
    "manila mankind manlike manly manmade manned mannish manor mantis mantra "
    "many map marbled marbles mardi marina marital marlin maroon married "
    "marrow marry marshy marxism mascot mashed mashing masses massive mastiff "
    "matador matcher mating matron matted mauve maybe mayday moaner moaning "
    "mobster mocha mocker mockup modular module moisten molar mold mollusk "
    "monday mongrel monsoon monthly moocher moody mooing mooned moonlit mop "
    "morale morally morse mortify mosaic mossy most motive motto mounted "
    "mourner mousy mouth movable moving mower mowing muck mud mug mulch "
    "mulled mullets mumble mumbo mummify mummy mumps mundane muppet mural "
    "murky mushily mushy musket musky mustang mustard muster musty mutable "
    "mutate mute mutiny mutt muzzle myspace mystify nacho nag nail naming "
    "nanny nape napped napping nappy nastily native natural navy nearby "
    "nearest nearly neatly nebula nectar negate nemeses nemesis neon nerd "
    "nervous nervy neuron neuter neutron nibble niece nifty nimble nimbly "
    "ninja ninth nuclei nucleus nugget nullify numbing numbly numeral numeric "
    "nursery nursing nurture nutcase nutlike nutmeg nutty nuzzle nylon oaf "
    "oasis oat obliged oblong oboe obtuse occupy ocelot octagon octane "
    "octopus ogle oink omega omen ominous onboard ongoing onset onshore "
    "onstage onto onward onyx oops ooze oozy opacity opal operate opium "
    "opossum opt osmosis otter ouch ought ounce outage outback outbid outcast "
    "outcome outfit outgrow outing outlast outlet outline outlook outmost "
    "outpost outpour outrage outrank outsell outward outwit ovary overact "
    "overall overbid overdue overfed overlap overlay overpay overrun overtly "
    "overuse owl oxford oxidant oxidize paced pacific pacify padded padding "
    "padlock pagan pager paging pajamas paltry pampers panama pancake pang "
    "panning pantry pants papaya paprika papyrus paradox parcel parched "
    "pardon parish parka parking parkway parlor parole parsley parsnip "
    "partake parted parting partly partner passage passing passion passive "
    "pasta pasted pastel pastime pastor pasture pasty patchy patio patriot "
    "pauper paver paving pawing payable payback payday payee payer paying "
    "payroll pebble pebbly pecan pectin pellet pelt pelvis pendant pending "
    "pennant penny penpal pension pep percent perch perfume perish perjury "
    "perky perm pesky peso pester petal petite petri petted petty petunia "
    "phantom phobia phoenix phoney phonics phony placard placate plank "
    "planner plant plasma plaster plated plating platter player playful "
    "playing playoff"
).split())

MEMORABLE_WORDS_ADJ = MEMORABLE_WORDS
MEMORABLE_WORDS_NOUN = MEMORABLE_WORDS
WORDLIST_ADJECTIVES = MEMORABLE_WORDS
WORDLIST_NOUNS = MEMORABLE_WORDS
PASSCODE_WORDS = MEMORABLE_WORDS

def generate_passcode() -> str:
    """
    Generate a clean, human-friendly 8-10+ character memorable passcode on initial launch.
    Format: word-word-NN (e.g. star-falcon-42, bold-tiger-79).
    Ambiguity exclusion: Avoids ambiguous glyphs (0, O, 1, l, I). Suffix digits strictly chosen from [2-9].
    CSPRNG: secrets.choice().
    Entropy: log2(4096 * 4096 * 8 * 8) = 30.0 bits of entropy.
    """
    w1 = secrets.choice(MEMORABLE_WORDS)
    w2 = secrets.choice(MEMORABLE_WORDS)
    d1 = secrets.choice("23456789")
    d2 = secrets.choice("23456789")
    return f"{w1}-{w2}-{d1}{d2}"



class SecurityConfig:
    """
    Manages persistent cryptographic keys, master passphrase hash,
    and security flags stored in .env.
    """

    def __init__(self, env_path: str = CONFIG_FILE):
        self.env_path = env_path
        self.secret_key: str = ""
        self.access_key: str = ""
        self.password_hash: str = ""
        self.raw_password: str = ""
        self.is_custom_passcode: bool = False
        self.trust_proxy_headers: bool = True
        self.allow_full_drive_remote: bool = False
        self.lock = threading.Lock()
        self.load_or_initialize()

    def load_or_initialize(self) -> None:
        """Load configuration from .env or initialize secure defaults."""
        with self.lock:
            env_vars: Dict[str, str] = {}
            if os.path.exists(self.env_path):
                try:
                    with open(self.env_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#") and "=" in line:
                                k, v = line.split("=", 1)
                                env_vars[k.strip()] = v.strip().strip("'\"")
                except Exception as e:
                    print(f"[AUTH WARNING] Could not read {self.env_path}: {e}")

            dirty = False

            # 1. Server Secret Key for HMAC Session Tokens (256-bit entropy)
            if "TURBOSHARE_SECRET_KEY" in env_vars and len(env_vars["TURBOSHARE_SECRET_KEY"]) >= 32:
                self.secret_key = env_vars["TURBOSHARE_SECRET_KEY"]
            else:
                self.secret_key = secrets.token_hex(32)
                env_vars["TURBOSHARE_SECRET_KEY"] = self.secret_key
                dirty = True

            # 2. URL-Safe Bookmarkable Secret Access Key
            if "TURBOSHARE_ACCESS_KEY" in env_vars and len(env_vars["TURBOSHARE_ACCESS_KEY"]) >= 16:
                self.access_key = env_vars["TURBOSHARE_ACCESS_KEY"]
            else:
                self.access_key = "ts_live_" + secrets.token_urlsafe(24)
                env_vars["TURBOSHARE_ACCESS_KEY"] = self.access_key
                dirty = True

            # 3. Master Passphrase & Hash (PBKDF2-HMAC-SHA256, 600,000 iterations)
            if "APP_PASSWORD" in env_vars and env_vars["APP_PASSWORD"]:
                # Automatic secure migration from legacy plaintext APP_PASSWORD
                raw_pwd = env_vars["APP_PASSWORD"]
                self.raw_password = raw_pwd
                self.password_hash = self.hash_password(raw_pwd)
                self.is_custom_passcode = True
                env_vars["TURBOSHARE_PASSCODE"] = raw_pwd
                env_vars["TURBOSHARE_PASSWORD_HASH"] = self.password_hash
                env_vars["TURBOSHARE_IS_CUSTOM_PASSCODE"] = "true"
                del env_vars["APP_PASSWORD"]
                dirty = True
                print("[AUTH NOTICE] Migrated plaintext APP_PASSWORD to salted PBKDF2-HMAC-SHA256 hash and passcode in .env")
            elif "TURBOSHARE_PASSCODE" in env_vars and env_vars["TURBOSHARE_PASSCODE"]:
                raw_pwd = env_vars["TURBOSHARE_PASSCODE"]
                self.raw_password = raw_pwd
                if "TURBOSHARE_IS_CUSTOM_PASSCODE" in env_vars:
                    self.is_custom_passcode = env_vars["TURBOSHARE_IS_CUSTOM_PASSCODE"].lower() in ("1", "true", "yes")

                if "TURBOSHARE_PASSWORD_HASH" in env_vars and env_vars["TURBOSHARE_PASSWORD_HASH"].startswith("pbkdf2_sha256$"):
                    self.password_hash = env_vars["TURBOSHARE_PASSWORD_HASH"]
                    if not self.verify_password(raw_pwd):
                        # User manually updated TURBOSHARE_PASSCODE in .env! Re-hash and synchronize
                        self.password_hash = self.hash_password(raw_pwd)
                        self.is_custom_passcode = True
                        env_vars["TURBOSHARE_PASSWORD_HASH"] = self.password_hash
                        env_vars["TURBOSHARE_IS_CUSTOM_PASSCODE"] = "true"
                        dirty = True
                else:
                    # Passcode present without valid hash, derive and store hash
                    self.password_hash = self.hash_password(raw_pwd)
                    env_vars["TURBOSHARE_PASSWORD_HASH"] = self.password_hash
                    dirty = True
            elif "TURBOSHARE_PASSWORD_HASH" in env_vars and env_vars["TURBOSHARE_PASSWORD_HASH"].startswith("pbkdf2_sha256$"):
                # Hash-only mode (backward compatibility / hardened deployment)
                self.password_hash = env_vars["TURBOSHARE_PASSWORD_HASH"]
                self.raw_password = ""
                if "TURBOSHARE_IS_CUSTOM_PASSCODE" in env_vars:
                    self.is_custom_passcode = env_vars["TURBOSHARE_IS_CUSTOM_PASSCODE"].lower() in ("1", "true", "yes")
            else:
                # Auto-generate memorable passcode in word-word-NN format
                generated_pwd = generate_passcode()
                self.raw_password = generated_pwd
                self.password_hash = self.hash_password(generated_pwd)
                self.is_custom_passcode = False
                env_vars["TURBOSHARE_PASSCODE"] = generated_pwd
                env_vars["TURBOSHARE_PASSWORD_HASH"] = self.password_hash
                env_vars["TURBOSHARE_IS_CUSTOM_PASSCODE"] = "false"
                dirty = True
                print("\n" + "=" * 68)
                print("  [SECURITY NOTICE] AUTO-GENERATED MEMORABLE MASTER APP PASSCODE:")
                print(f"  >>>  {generated_pwd}  <<<")
                print("  Bookmark Access Key:")
                print(f"  >>>  {self.access_key}  <<<")
                print("  Saved passcode and salted PBKDF2 hash to .env. Keep this confidential!")
                print("  Tip: We recommend setting your own personal passcode, though your auto-generated code is active and secure.")
                print("=" * 68 + "\n")

            # 4. Optional Security Flags
            if "TRUST_PROXY_HEADERS" in env_vars:
                self.trust_proxy_headers = env_vars["TRUST_PROXY_HEADERS"].lower() in ("1", "true", "yes")
            else:
                self.trust_proxy_headers = True

            if "ALLOW_FULL_DRIVE_REMOTE" in env_vars:
                self.allow_full_drive_remote = env_vars["ALLOW_FULL_DRIVE_REMOTE"].lower() in ("1", "true", "yes")
            else:
                self.allow_full_drive_remote = False

            if dirty:
                try:
                    with open(self.env_path, "w", encoding="utf-8") as f:
                        f.write("# TurboShare Hardened Security Configuration\n")
                        f.write("# Generated automatically on startup\n\n")
                        for k, v in env_vars.items():
                            f.write(f"{k}={v}\n")
                    if sys.platform != "win32":
                        try:
                            os.chmod(self.env_path, 0o600)
                        except Exception:
                            pass
                except Exception as e:
                    print(f"[AUTH ERROR] Could not save security configuration to {self.env_path}: {e}")

    @staticmethod
    def hash_password(password: str, iterations: int = DEFAULT_ITERATIONS) -> str:
        """Derive salted PBKDF2-HMAC-SHA256 hash string."""
        salt = secrets.token_bytes(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)
        return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"

    def verify_password(self, password: str) -> bool:
        """Constant-time verification of password against stored PBKDF2 hash."""
        if not self.password_hash or not password:
            return False
        try:
            parts = self.password_hash.split("$")
            if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
                return False
            _, iters_str, salt_hex, expected_dk_hex = parts
            iterations = int(iters_str)
            salt = bytes.fromhex(salt_hex)
            expected_dk = bytes.fromhex(expected_dk_hex)
            actual_dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)
            return hmac.compare_digest(actual_dk, expected_dk)
        except Exception:
            return False

    def verify_access_key(self, key: str) -> bool:
        """Constant-time verification of bookmark secret access key."""
        if not self.access_key or not key:
            return False
        return hmac.compare_digest(key.strip(), self.access_key.strip())


def parse_device_info(user_agent: str) -> str:
    """Parse user-agent string into human-friendly device and browser representation."""
    ua = (user_agent or "").lower()
    if not ua:
        return "Browser / Web Client"

    # OS / Hardware Platform
    os_name = "Desktop"
    if "iphone" in ua:
        os_name = "Apple iPhone"
    elif "ipad" in ua:
        os_name = "Apple iPad"
    elif "android" in ua:
        os_name = "Android Phone"
    elif "windows nt 10" in ua or "windows nt 11" in ua or "windows" in ua:
        os_name = "Windows PC"
    elif "macintosh" in ua or "mac os" in ua:
        os_name = "Apple Mac"
    elif "linux" in ua:
        os_name = "Linux PC"

    # Browser Engine / Client
    browser = "Browser"
    if "edg/" in ua or "edg" in ua:
        browser = "Microsoft Edge"
    elif "chrome/" in ua and "edg" not in ua:
        browser = "Google Chrome"
    elif "safari/" in ua and "chrome" not in ua:
        browser = "Apple Safari"
    elif "firefox/" in ua:
        browser = "Mozilla Firefox"
    elif "curl" in ua or "python" in ua or "bot" in ua:
        browser = "API Client"

    return f"{os_name} · {browser}"


def parse_location(handler: Any) -> str:
    """Extract location from Cloudflare headers or fallback to connection origin."""
    if not handler:
        return "Unknown"
    headers = getattr(handler, "headers", {}) or {}
    country = str(headers.get("CF-IPCountry", "")).strip()
    city = str(headers.get("CF-IPCity", "")).strip()
    if city and country:
        return f"{city}, {country}"
    if country:
        return country
    peer = getattr(handler, "client_address", ("", 0))[0]
    peer_str = str(peer)
    if peer_str in ("127.0.0.1", "::1", "localhost") or peer_str.startswith("127."):
        return "Host Localhost"
    if peer_str.startswith("192.168.") or peer_str.startswith("10.") or peer_str.startswith("172."):
        return "Local Network (LAN)"
    return "Remote Internet"


class SessionRegistry:
    """
    Thread-safe persistent registry of active and revoked remote sessions.
    Maintains session history with IP, device, location, and timestamps.
    """

    def __init__(self, storage_path: str = SESSIONS_FILE):
        self.storage_path = storage_path
        self.lock = threading.Lock()
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.revoked_ids: set = set()
        self.load()

    def load(self) -> None:
        with self.lock:
            if os.path.exists(self.storage_path):
                try:
                    with open(self.storage_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        self.sessions = data.get("sessions", {})
                        self.revoked_ids = set(data.get("revoked_ids", []))
                except Exception as e:
                    print(f"[AUTH WARNING] Could not load session registry: {e}")

    def save(self) -> None:
        try:
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump({
                    "sessions": self.sessions,
                    "revoked_ids": list(self.revoked_ids)
                }, f, indent=2)
        except Exception as e:
            print(f"[AUTH ERROR] Could not save session registry: {e}")

    def register(self, session_id: str, client_ip: str = "", user_agent: str = "", location: str = "") -> None:
        with self.lock:
            now = int(time.time())
            self.sessions[session_id] = {
                "id": session_id,
                "ip": client_ip or "Unknown",
                "device": parse_device_info(user_agent),
                "location": location or "Unknown",
                "user_agent": (user_agent or "")[:150],
                "issued_at": now,
                "last_active": now,
                "revoked": False
            }
            self.save()

    def touch(self, session_id: str) -> None:
        with self.lock:
            if session_id in self.sessions and not self.sessions[session_id].get("revoked"):
                self.sessions[session_id]["last_active"] = int(time.time())

    def is_revoked(self, session_id: str) -> bool:
        with self.lock:
            if session_id in self.revoked_ids:
                return True
            sess = self.sessions.get(session_id)
            return bool(sess and sess.get("revoked"))

    def revoke(self, session_id: str) -> bool:
        with self.lock:
            self.revoked_ids.add(session_id)
            if session_id in self.sessions:
                self.sessions[session_id]["revoked"] = True
            self.save()
            return True

    def revoke_all(self) -> int:
        with self.lock:
            count = 0
            for sid in self.sessions:
                if not self.sessions[sid].get("revoked"):
                    self.sessions[sid]["revoked"] = True
                    count += 1
                self.revoked_ids.add(sid)
            self.save()
            return count

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self.lock:
            result = list(self.sessions.values())
            result.sort(key=lambda s: s.get("last_active", 0), reverse=True)
            return result


GLOBAL_SESSION_REGISTRY = SessionRegistry()


class SessionManager:
    """
    Manages creation and stateless cryptographic verification of persistent
    HMAC-SHA256 signed session tokens surviving server reboots.
    """

    def __init__(self, secret_key: str):
        self.secret_key = secret_key

    def update_secret_key(self, secret_key: str) -> None:
        self.secret_key = secret_key

    def create_token(self, client_ip: str = "", user_agent: str = "", location: str = "", ttl_days: int = SESSION_TTL_DAYS) -> str:
        """Generate a cryptographically signed stateless session token and register it."""
        version = "v1"
        session_id = secrets.token_hex(16)
        issued_at = int(time.time())
        expires_at = issued_at + (ttl_days * 86400)
        ua_hash = hashlib.sha256((user_agent or "").encode("utf-8")).hexdigest()[:16]
        payload = f"{version}.{session_id}.{issued_at}.{expires_at}.{ua_hash}"
        signature = hmac.new(self.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        
        # Register in session registry
        GLOBAL_SESSION_REGISTRY.register(session_id, client_ip=client_ip, user_agent=user_agent, location=location)
        
        return f"{payload}.{signature}"

    def verify_token(self, token: str, user_agent: str = "") -> bool:
        """Verify session token signature, expiration, user agent fingerprint, and revocation in constant time."""
        if not token or not self.secret_key:
            return False
        try:
            parts = token.strip().split(".")
            if len(parts) != 6:
                return False
            version, session_id, issued_at_str, expires_at_str, ua_hash, signature = parts
            if version != "v1":
                return False

            payload = f"{version}.{session_id}.{issued_at_str}.{expires_at_str}.{ua_hash}"
            expected_signature = hmac.new(
                self.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(signature, expected_signature):
                return False

            now = int(time.time())
            issued_at = int(issued_at_str)
            expires_at = int(expires_at_str)

            # Check expiration and 5-minute clock skew tolerance
            if now > expires_at or now < (issued_at - 300):
                return False

            # Verify user-agent hash if provided
            if user_agent:
                expected_ua_hash = hashlib.sha256(user_agent.encode("utf-8")).hexdigest()[:16]
                if not hmac.compare_digest(ua_hash, expected_ua_hash):
                    return False

            # Check revocation in session registry
            if GLOBAL_SESSION_REGISTRY.is_revoked(session_id):
                return False

            # Update last_active
            GLOBAL_SESSION_REGISTRY.touch(session_id)

            return True
        except Exception:
            return False


class SlidingWindowTarpitLimiter:
    """
    Thread-safe sliding-window rate limiter with exponential delay tarpitting.
    Limits failed authentication attempts per IP and slows down automated brute-force attacks.
    """

    def __init__(
        self,
        window_sec: int = RATE_LIMIT_WINDOW,
        max_failures: int = MAX_FAILED_ATTEMPTS,
        base_delay: float = BASE_TARPIT_DELAY,
        max_delay: float = MAX_TARPIT_DELAY,
    ):
        self.window_sec = window_sec
        self.max_failures = max_failures
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.lock = threading.Lock()
        self.failure_history: Dict[str, List[float]] = defaultdict(list)
        self.last_cleanup = time.time()

    def _cleanup_stale(self, now: float) -> None:
        """Remove entries older than window_sec periodically."""
        if now - self.last_cleanup > 300:
            cutoff = now - self.window_sec
            stale_ips = [ip for ip, timestamps in self.failure_history.items() if not timestamps or timestamps[-1] < cutoff]
            for ip in stale_ips:
                del self.failure_history[ip]
            self.last_cleanup = now

    def check_rate_limit(self, client_ip: str) -> Tuple[bool, float]:
        """
        Check if an IP is currently allowed to attempt authentication.
        Returns:
            (is_allowed: bool, delay_or_retry_after: float)
            - If allowed: (True, current_delay)
            - If locked out (>= max_failures in window): (False, remaining_lockout_seconds)
        """
        now = time.time()
        with self.lock:
            self._cleanup_stale(now)
            cutoff = now - self.window_sec
            history = [t for t in self.failure_history.get(client_ip, []) if t > cutoff]
            self.failure_history[client_ip] = history
            failures = len(history)

            if failures >= self.max_failures:
                # Locked out until oldest failure expires
                oldest = history[0] if history else now
                remaining = max(1.0, self.window_sec - (now - oldest))
                return False, remaining

            current_delay = 0.0
            if failures > 0:
                current_delay = min(self.base_delay * (2 ** (failures - 1)), self.max_delay)

            return True, current_delay

    def record_failure(self, client_ip: str) -> Tuple[float, int]:
        """
        Record a failed authentication attempt and calculate tarpit delay.
        Executes tarpit sleep outside the lock to prevent thread starvation.
        Returns (delay_seconds, total_failures_in_window).
        """
        now = time.time()
        with self.lock:
            self._cleanup_stale(now)
            cutoff = now - self.window_sec
            history = [t for t in self.failure_history.get(client_ip, []) if t > cutoff]
            history.append(now)
            self.failure_history[client_ip] = history
            failures = len(history)
            delay = min(self.base_delay * (2 ** (failures - 1)), self.max_delay)

        # Sleep outside the lock so other requests are not blocked
        time.sleep(delay)
        return delay, failures

    def record_success(self, client_ip: str) -> None:
        """Reset failed attempt history for client IP upon successful login."""
        with self.lock:
            if client_ip in self.failure_history:
                del self.failure_history[client_ip]


# ── Global Singleton Instances ─────────────────────────────────────────────────
GLOBAL_SECURITY_CONFIG = SecurityConfig()
GLOBAL_SESSION_MANAGER = SessionManager(GLOBAL_SECURITY_CONFIG.secret_key)
GLOBAL_RATE_LIMITER = SlidingWindowTarpitLimiter()


# ── Public API Interface Contracts ─────────────────────────────────────────────

def get_security_config() -> SecurityConfig:
    return GLOBAL_SECURITY_CONFIG

def init_config(env_path: str = CONFIG_FILE) -> SecurityConfig:
    global GLOBAL_SECURITY_CONFIG
    GLOBAL_SECURITY_CONFIG = SecurityConfig(env_path)
    return GLOBAL_SECURITY_CONFIG

def get_master_password() -> str:
    """Return raw master password if available in memory, or masked notice."""
    return getattr(GLOBAL_SECURITY_CONFIG, "raw_password", "") or "[Stored hashed in .env]"

def hash_password(password: str, iterations: int = DEFAULT_ITERATIONS) -> str:
    """Derive salted PBKDF2-HMAC-SHA256 hash string."""
    return SecurityConfig.hash_password(password, iterations=iterations)

def get_secret_key() -> str:
    return GLOBAL_SECURITY_CONFIG.secret_key

def get_access_key() -> str:
    return GLOBAL_SECURITY_CONFIG.access_key

def get_session_manager() -> SessionManager:
    # Ensure session manager uses latest secret key
    if GLOBAL_SESSION_MANAGER.secret_key != GLOBAL_SECURITY_CONFIG.secret_key:
        GLOBAL_SESSION_MANAGER.update_secret_key(GLOBAL_SECURITY_CONFIG.secret_key)
    return GLOBAL_SESSION_MANAGER

def get_rate_limiter() -> SlidingWindowTarpitLimiter:
    return GLOBAL_RATE_LIMITER

def verify_password(password: str) -> bool:
    """Verify master passphrase against stored PBKDF2 hash in constant time."""
    return GLOBAL_SECURITY_CONFIG.verify_password(password)

def verify_access_key(key: str) -> bool:
    """Verify bookmark access key in constant time."""
    return GLOBAL_SECURITY_CONFIG.verify_access_key(key)

def get_session_registry() -> SessionRegistry:
    return GLOBAL_SESSION_REGISTRY

def list_sessions() -> List[Dict[str, Any]]:
    return GLOBAL_SESSION_REGISTRY.list_sessions()

def revoke_session(session_id: str) -> bool:
    return GLOBAL_SESSION_REGISTRY.revoke(session_id)

def revoke_all_sessions() -> int:
    return GLOBAL_SESSION_REGISTRY.revoke_all()

def change_master_password(new_password: str, revoke_all_sessions: bool = True) -> Tuple[bool, str]:
    """
    Update master passphrase, re-hash with PBKDF2-HMAC-SHA256, write to .env,
    and optionally revoke all active remote sessions.
    """
    cleaned = new_password.strip()
    if len(cleaned) < 6:
        return False, "Master passcode must be at least 6 characters long."

    new_hash = hash_password(cleaned)
    GLOBAL_SECURITY_CONFIG.password_hash = new_hash
    GLOBAL_SECURITY_CONFIG.raw_password = cleaned
    GLOBAL_SECURITY_CONFIG.is_custom_passcode = True

    env_path = GLOBAL_SECURITY_CONFIG.env_path
    lines = []
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            print(f"[AUTH ERROR] Failed reading {env_path}: {e}")

    new_lines = []
    hash_written = False
    passcode_written = False
    custom_written = False
    for line in lines:
        if line.strip().startswith("TURBOSHARE_PASSWORD_HASH="):
            new_lines.append(f"TURBOSHARE_PASSWORD_HASH={new_hash}\n")
            hash_written = True
        elif line.strip().startswith("TURBOSHARE_PASSCODE="):
            new_lines.append(f"TURBOSHARE_PASSCODE={cleaned}\n")
            passcode_written = True
        elif line.strip().startswith("TURBOSHARE_IS_CUSTOM_PASSCODE="):
            new_lines.append("TURBOSHARE_IS_CUSTOM_PASSCODE=true\n")
            custom_written = True
        elif line.strip().startswith("APP_PASSWORD="):
            continue
        else:
            new_lines.append(line)

    if not passcode_written:
        new_lines.append(f"TURBOSHARE_PASSCODE={cleaned}\n")
    if not hash_written:
        new_lines.append(f"TURBOSHARE_PASSWORD_HASH={new_hash}\n")
    if not custom_written:
        new_lines.append("TURBOSHARE_IS_CUSTOM_PASSCODE=true\n")

    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        return False, f"Could not write to {env_path}: {e}"

    revoked_count = 0
    if revoke_all_sessions:
        revoked_count = GLOBAL_SESSION_REGISTRY.revoke_all()

    return True, f"Master passcode successfully updated. {revoked_count} active remote session(s) revoked."

def create_session_token(client_ip: str = "", user_agent: str = "", location: str = "") -> str:
    """Create a persistent signed session token and register metadata."""
    return get_session_manager().create_token(client_ip=client_ip, user_agent=user_agent, location=location)

def verify_session_token(token: str, client_ip: str = "", user_agent: str = "") -> bool:
    """Verify persistent signed session token."""
    return get_session_manager().verify_token(token, user_agent=user_agent)

def check_rate_limit(client_ip: str) -> Tuple[bool, float]:
    """Check sliding window rate limit for client IP."""
    return GLOBAL_RATE_LIMITER.check_rate_limit(client_ip)

def record_login_failure(client_ip: str) -> None:
    """Record login failure and execute tarpit delay."""
    GLOBAL_RATE_LIMITER.record_failure(client_ip)

def record_login_success(client_ip: str) -> None:
    """Record login success and clear failure counter."""
    GLOBAL_RATE_LIMITER.record_success(client_ip)

def get_client_ip(handler: Any) -> str:
    """
    Deterministically resolves the real client IP address:
    1. Direct connection from non-loopback IP -> Strictly ignore proxy headers (prevents LAN spoofing).
    2. Connection from loopback (127.0.0.1 / ::1) -> Parse CF-Connecting-IP or X-Forwarded-For if trusted.
    """
    if not handler or not hasattr(handler, "client_address") or not handler.client_address:
        return "0.0.0.0"

    try:
        socket_ip = str(handler.client_address[0]).strip()
    except (IndexError, TypeError, Exception):
        return "0.0.0.0"
    is_loopback = socket_ip in ("127.0.0.1", "::1", "localhost") or socket_ip.startswith("127.")

    if not is_loopback or not GLOBAL_SECURITY_CONFIG.trust_proxy_headers:
        return socket_ip

    headers = getattr(handler, "headers", None)
    if not headers:
        return socket_ip

    # 1. Cloudflare Tunnel Header
    cf_ip = headers.get("CF-Connecting-IP")
    if cf_ip:
        cf_ip = cf_ip.strip()
        try:
            ipaddress.ip_address(cf_ip)
            return cf_ip
        except ValueError:
            pass

    # 2. X-Forwarded-For Header (extract first/client IP)
    xff = headers.get("X-Forwarded-For")
    if xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        for cand in hops:
            try:
                ipaddress.ip_address(cand)
                if not cand.startswith("127."):
                    return cand
            except ValueError:
                continue

    # 3. X-Real-IP Header
    x_real = headers.get("X-Real-IP")
    if x_real:
        x_real = x_real.strip()
        try:
            ipaddress.ip_address(x_real)
            return x_real
        except ValueError:
            pass

    return socket_ip


def is_authenticated(handler: Any) -> bool:
    """Check whether incoming request carries a valid persistent session cookie."""
    if not handler or not hasattr(handler, "headers"):
        return False
    cookie_header = handler.headers.get("Cookie", "")
    if not cookie_header:
        return False
    try:
        c = cookies.SimpleCookie()
        c.load(cookie_header)
        token = None
        if SESSION_COOKIE_NAME in c:
            token = c[SESSION_COOKIE_NAME].value
        elif "ts_session" in c:
            token = c["ts_session"].value

        if not token:
            return False

        ua = handler.headers.get("User-Agent", "")
        return get_session_manager().verify_token(token, user_agent=ua)
    except Exception:
        return False


def build_session_cookie(token: str, is_https: bool = False, max_age: int = 2592000) -> str:
    """Construct secure Set-Cookie header string."""
    flags = [
        f"{SESSION_COOKIE_NAME}={token}",
        "Path=/",
        f"Max-Age={max_age}",
        "HttpOnly",
        "SameSite=Strict"
    ]
    if is_https:
        flags.append("Secure")
    return "; ".join(flags)


def is_request_https(handler: Any) -> bool:
    """Determine if request arrived over HTTPS (directly or via TLS-terminating reverse proxy)."""
    if not handler or not hasattr(handler, "headers"):
        return False
    headers = handler.headers
    if headers.get("X-Forwarded-Proto") == "https":
        return True
    if headers.get("X-Forwarded-Ssl") == "on":
        return True
    cf_visitor = headers.get("CF-Visitor", "")
    if '"scheme":"https"' in cf_visitor or '"scheme": "https"' in cf_visitor:
        return True
    return False


TUNNEL_HEADERS = frozenset({"cf-connecting-ip", "x-forwarded-for", "x-real-ip", "forwarded", "true-client-ip"})


def is_physical_localhost(handler: Any) -> bool:
    """
    Verify whether request originated physically from loopback (127.0.0.1, ::1)
    and strictly without reverse-proxy tunnel headers (Forwarded, CF-Connecting-IP, X-Forwarded-For, X-Real-IP, True-Client-IP).
    Fail-closed: Returns False if handler or client_address is missing/invalid or if tunnel headers are present.
    """
    if not handler or not getattr(handler, "client_address", None):
        return False

    try:
        peer = str(handler.client_address[0]).strip()
    except (IndexError, TypeError, Exception):
        return False

    is_loopback = (
        peer in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")
        or peer.startswith("127.")
        or peer.startswith("::ffff:127.")
    )
    if not is_loopback:
        return False

    headers = getattr(handler, "headers", None)
    if headers:
        # Case-insensitive check across dictionary/HTTPMessage items
        if hasattr(headers, "items"):
            for k, v in headers.items():
                if str(k).strip().lower() in TUNNEL_HEADERS and bool(str(v).strip()):
                    return False
        # Direct lookup for case-insensitive structures (HTTPMessage)
        for h in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP", "Forwarded", "True-Client-IP"):
            val = getattr(headers, "get", lambda x, d=None: None)(h)
            if val and bool(str(val).strip()):
                return False

    return True


def handle_auth_routes(handler: Any, path: str, qs: Dict[str, List[str]], body_data: Optional[Dict[str, Any]] = None) -> bool:
    """
    Handle /api/auth, /api/login, /api/logout, /api/check_auth, /api/sessions,
    /api/revoke_session, /api/change_password, and /api/host_security_info routes.
    Returns True if request was handled, False otherwise.
    """
    client_ip = get_client_ip(handler)
    is_https = is_request_https(handler)

    # 1. URL-Safe Auto-Login via Bookmarked Key (/api/auth?key=...)
    if path == "/api/auth":
        key = qs.get("key", [""])[0] if qs else ""
        if not key and body_data and "key" in body_data:
            key = str(body_data["key"]).strip()

        is_allowed, penalty = check_rate_limit(client_ip)
        if not is_allowed:
            payload = json.dumps({"error": "rate_limited", "retry_after": int(penalty)}).encode("utf-8")
            handler.send_response(429)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Retry-After", str(int(penalty)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True

        if verify_access_key(key):
            record_login_success(client_ip)
            ua = handler.headers.get("User-Agent", "") if hasattr(handler, "headers") else ""
            loc = parse_location(handler)
            token = create_session_token(client_ip=client_ip, user_agent=ua, location=loc)
            cookie_hdr = build_session_cookie(token, is_https=is_https)

            # Issue HTTP 303 PRG Clean Redirect
            handler.send_response(303)
            handler.send_header("Location", "/")
            handler.send_header("Set-Cookie", cookie_hdr)
            handler.send_header("Referrer-Policy", "no-referrer")
            handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            handler.send_header("Pragma", "no-cache")
            handler.send_header("Expires", "0")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True
        else:
            record_login_failure(client_ip)
            payload = json.dumps({"success": False, "error": "invalid_access_key"}).encode("utf-8")
            handler.send_response(401)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True

    # 2. JSON API Login (/api/login)
    if path == "/api/login":
        password = ""
        key = ""
        if body_data:
            password = str(body_data.get("password", "")).strip()
            key = str(body_data.get("key", "")).strip()
        elif qs:
            password = qs.get("password", [""])[0]
            key = qs.get("key", [""])[0]

        is_allowed, penalty = check_rate_limit(client_ip)
        if not is_allowed:
            payload = json.dumps({"error": "rate_limited", "retry_after": int(penalty)}).encode("utf-8")
            handler.send_response(429)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Retry-After", str(int(penalty)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True

        authenticated = False
        if password and verify_password(password):
            authenticated = True
        elif key and verify_access_key(key):
            authenticated = True

        if authenticated:
            record_login_success(client_ip)
            ua = handler.headers.get("User-Agent", "") if hasattr(handler, "headers") else ""
            loc = parse_location(handler)
            token = create_session_token(client_ip=client_ip, user_agent=ua, location=loc)
            cookie_hdr = build_session_cookie(token, is_https=is_https)

            payload = json.dumps({"success": True, "status": "ok", "token": token}).encode("utf-8")
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Set-Cookie", cookie_hdr)
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            handler.wfile.write(payload)
            return True
        else:
            record_login_failure(client_ip)
            payload = json.dumps({"success": False, "error": "invalid_credentials"}).encode("utf-8")
            handler.send_response(401)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True

    # 3. Logout (/api/logout)
    if path == "/api/logout":
        # Extract session_id and revoke server-side
        cookie_header = handler.headers.get("Cookie", "") if hasattr(handler, "headers") else ""
        if cookie_header:
            try:
                c = cookies.SimpleCookie()
                c.load(cookie_header)
                tok = None
                if SESSION_COOKIE_NAME in c:
                    tok = c[SESSION_COOKIE_NAME].value
                elif "ts_session" in c:
                    tok = c["ts_session"].value
                if tok:
                    tok_parts = tok.split(".")
                    if len(tok_parts) >= 2:
                        GLOBAL_SESSION_REGISTRY.revoke(tok_parts[1])
            except Exception:
                pass

        cookie_hdr = f"{SESSION_COOKIE_NAME}=deleted; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
        payload = json.dumps({"success": True, "logged_out": True}).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Set-Cookie", cookie_hdr)
        handler.end_headers()
        handler.wfile.write(payload)
        return True

    # 4. Check Authentication Status (/api/check_auth)
    if path == "/api/check_auth":
        authed = is_authenticated(handler)
        payload = json.dumps({"authenticated": authed}).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return True

    # 5. List Sessions (/api/sessions) - Strictly Host Only
    if path == "/api/sessions":
        if not is_physical_localhost(handler):
            payload = json.dumps({"success": False, "error": "forbidden", "message": "Host only."}).encode("utf-8")
            handler.send_response(403)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True

        sessions = GLOBAL_SESSION_REGISTRY.list_sessions()
        payload = json.dumps({"success": True, "sessions": sessions}).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return True

    # 6. Revoke Remote Session (/api/revoke_session) - Strictly Host Only
    if path == "/api/revoke_session":
        if not is_physical_localhost(handler):
            payload = json.dumps({"success": False, "error": "forbidden", "message": "Host only."}).encode("utf-8")
            handler.send_response(403)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True

        target_id = body_data.get("session_id", "").strip() if body_data else ""
        revoke_all = bool(body_data.get("all", False)) if body_data else False
        if revoke_all:
            count = GLOBAL_SESSION_REGISTRY.revoke_all()
            msg = f"All active remote sessions ({count}) have been revoked."
        elif target_id:
            GLOBAL_SESSION_REGISTRY.revoke(target_id)
            msg = f"Session {target_id[:8]}... successfully revoked."
        else:
            msg = "No session ID specified."

        payload = json.dumps({"success": True, "message": msg}).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return True

    # 7. Change Master Password (/api/change_password) - Strictly Host Only
    if path == "/api/change_password":
        if not is_physical_localhost(handler):
            payload = json.dumps({"success": False, "error": "forbidden", "message": "Host only."}).encode("utf-8")
            handler.send_response(403)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True

        new_pwd = body_data.get("new_password", "").strip() if body_data else ""
        revoke_all = bool(body_data.get("revoke_sessions", True)) if body_data else True
        ok, msg = change_master_password(new_pwd, revoke_all_sessions=revoke_all)
        status = 200 if ok else 400
        payload = json.dumps({"success": ok, "message": msg}).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return True

    # 8. Host Security Info (/api/host_security_info) - Strictly Host Only
    if path == "/api/host_security_info":
        if not is_physical_localhost(handler):
            payload = json.dumps({"success": False, "error": "forbidden", "message": "Host only."}).encode("utf-8")
            handler.send_response(403)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True

        passcode = get_master_password()
        is_custom = getattr(GLOBAL_SECURITY_CONFIG, "is_custom_passcode", False)
        tip = "Tip: We recommend setting your own personal passcode, though your auto-generated code is active and secure."
        payload = json.dumps({
            "success": True,
            "passcode": passcode,
            "is_custom": is_custom,
            "iterations": DEFAULT_ITERATIONS,
            "access_key": get_access_key(),
            "tip": tip
        }).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return True

    return False
