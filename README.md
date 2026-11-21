# SyncShallot

<img src="syncshallot/assets/syncshallot.png" width="80" alt="SyncShallot-sjalotmascotte">

**De gemakkelijkste manier om je backup schijven te synchroniseren, zonder onnodige bestanden op je backups te laten staan!**

Wanneer je bestanden op je hoofdschijf vervangt of verwijdert, blijven de oude versies vaak op je back-upschijf staan. Denk aan een mediaverzameling waarin een video met een hogere resolutie de oude versie vervangt: de nieuwe video wordt naar de back-up gekopieerd, maar de oude video neemt daar nog steeds ruimte in.

SyncShallot vergelijkt de bron en de back-up inclusief submappen. Het kopieert nieuwe of gewijzigde bestanden en laat duidelijk zien welke bestanden alleen nog op de back-up staan. Je kiest zelf of je die laat staan, in een aparte map isoleert of verwijdert. De bron blijft standaard beschermd. Of de gekozen mappen op een lokale schijf, USB-schijf of gemounte netwerkshare staan, maakt daarbij niet uit.

De twee panelen tonen overeenkomstige bestanden naast elkaar, met een overzicht van verschillen en geplande acties. De vormgeving sluit aan op PixelPickle en SoundSausage; het bureaubladthema bepaalt de kleuren. Dit is de eerste ontwikkelversie, **0.1.0**.

## Beginnen

Deze eerste ontwikkelversie vereist Python 3.11 of nieuwer en PySide6 6.6 of nieuwer. Vanuit deze projectmap:

```bash
./syncshallot.sh
```

Als PySide6 nog niet beschikbaar is, kun je de app in een virtuele omgeving installeren:

```bash
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/syncshallot
```

De launcher gebruikt een aanwezige `.venv` automatisch. Een installeerbaar Python-wheel staat na het bouwen in `dist/`. Distributiepakketten en publicatie volgen afzonderlijk.

## Netwerkshare naar USB-back-up

1. Kies links de bronmap en rechts de doelmap. Dat mogen lokale mappen of al gemounte USB-, SMB- of NFS-locaties zijn; SyncShallot mount zelf geen schijven en vraagt geen netwerkgegevens.
2. Laat **Links → rechts** staan. Links is beschermd en SyncShallot schrijft daar niet.
3. Kies **Grondig** voor inhoudscontrole, of **Snel** voor alleen grootte en wijzigingsdatum. Bij snel vergelijken geldt twee seconden datumtolerantie; verschillende bestanden kunnen daardoor als gelijk worden beschouwd. Grondig vergelijkt SHA-256 van de volledige inhoud en biedt meer zekerheid, zonder absolute foutloosheid te beloven.
4. Klik op **Vergelijken**. Vergelijken wijzigt geen bestanden. Verborgen bestanden en lege mappen worden meegenomen. Een scan die mislukt levert geen uitvoerbaar plan op.
5. Nieuwe en gewijzigde bestanden krijgen automatisch een kopieeractie. Met **Extra op doel** zie je bestanden die uitsluitend op het doel staan, bijvoorbeeld een oude 720p-film waarvan de bron alleen nog een 4K-versie bevat.
6. Selecteer extra rijen (Ctrl/Shift of Ctrl+A binnen het filter). Kies **Isoleren** of **Definitief verwijderen** en klik **Actie instellen**. De actie op een map geldt ook voor de onderliggende rijen. Niet gekozen extra bestanden blijven staan.
7. Controleer via **Acties controleren…** de richtingen, alle paden en de aantallen nieuwe bestanden, overschrijvingen, isolaties en verwijderingen. Vink de bevestiging aan. Definitief verwijderen vereist daarnaast exact `DELETE`.
8. Na uitvoering vergelijkt het programma opnieuw. Als alles overeenkomt, staan er geen resterende verschillen. Kies hiervoor een grondige scan wanneer je inhoudelijke gelijkheid wilt controleren.

Inhoudelijk gelijk betekent dezelfde relatieve paden en gewone bestandsinhoud binnen de gekozen filters. Het is geen schijfimage of volledige kopie van bestandssysteemmetadata.

## Bescherming en herstel

- De linkermap is standaard beschermd. Voor omgekeerde of tweerichtingssynchronisatie moet je **bewust toestaan dat ook de linkermap wordt gewijzigd**. Het activeren van de optie voert zelf niets uit. In de omgekeerde richting is rechts de beschermde bron. De toestemming voor links wordt niet bewaard bij afsluiten, taalwissels of het openen van een profiel.
- In twee richtingen worden ontbrekende bestanden aangevuld. Bij verschillende bestanden kies je zelf links → rechts of rechts → links. Tijdstempels bepalen nooit automatisch welke versie wint. Verwijderingen worden niet doorgegeven; er is geen historie om een bewuste verwijdering van een nieuw bestand te onderscheiden.
- Overschrijven maakt altijd eerst een afzonderlijke, gecontroleerde herstelkopie. Daarna wordt de nieuwe inhoud op het doel geplaatst. Alle gekopieerde bestanden worden eerst tijdelijk op het doel geschreven en teruggelezen ter controle. Een bestaande doelversie wordt pas na die controle vervangen.
- **Isoleren** maakt een gecontroleerde kopie in de herstelmap en verwijdert vervolgens het extra bestand van het doel. De relatieve mappenstructuur blijft behouden, ook voor lege mappen.
- **Definitief verwijderen** gebruikt geen prullenbak en maakt geen herstelkopie van het verwijderde bestand. De bevestiging vermeldt dit expliciet.
- De herstelmap moet buiten beide gekozen mappen liggen. Standaard is dat `~/.local/share/SyncShallot/recovery`. Kies bij grote media eventueel een ruimere locatie in **Opties en herstel**. Een map naast de gekozen doelmap op dezelfde schijf kan ook, mits die buiten beide geselecteerde mappen ligt. Hersteldata wordt niet automatisch opgeschoond.
- Elke uitvoering krijgt een eigen map met `overwritten/left`, `overwritten/right`, `isolated/left` of `isolated/right`, en `journal.jsonl`. Via **Bestand → Herstelmap openen** kun je herstelkopieën bekijken en handmatig terugzetten; automatisch herstel is nog niet aanwezig. Het logboek bevat oorspronkelijke paden en uitgevoerde acties.
- Voor uitvoering worden beide bestandenlijsten opnieuw gecontroleerd. Een veranderd bestand, vervangen map of verdwenen mount blokkeert een verouderd plan. Een lege bron met geen gewone bestanden blokkeert opruimacties als extra bescherming tegen een verkeerd of niet gemount bronpad.
- Symbolische links, speciale bestanden en bestand/map-conflicten worden zichtbaar overgeslagen. De app volgt bij schrijven geen symbolische links. Een map met uitgesloten, niet geselecteerde of niet ondersteunde kinderen kan niet worden verwijderd.
- Bij een fout stopt de taak. Eerder voltooide acties blijven uitgevoerd; dit is geen transactie met automatisch terugdraaien. Een onderbroken bestandskopie laat de bestaande doelversie intact en tijdelijke bestanden worden bij een normale fout of annulering opgeruimd. Na een harde crash kan een `.syncshallot-*.tmp` overblijven; controleer die voordat je ze zelf verwijdert.
- Het programma controleert vrije ruimte vooraf, maar vrije ruimte, verbindingen en bestanden kunnen tijdens het werken veranderen. Het is geen beveiligingsgrens tegen andere processen die doelbewust gelijktijdig mappen verplaatsen. Zorg voor een onafhankelijke back-up en begin met proefmappen.

## Opties

Nederlands, Engels, Duits, Frans en Spaans zijn beschikbaar. De taalkeuze is rechtsboven; wisselen reset het vergelijkingsplan en de bronbescherming. Onder **Opties en herstel** staan uitsluitpatronen (één per regel, bijvoorbeeld `*.tmp` of `.git`). Profielen bewaren paden, scanmethode en filters; bij openen blijft links → rechts de veilige standaard. Vergelijkingen zijn als JSON te exporteren.

Bestandsinhoud, relatieve paden, gewone lees-/schrijfrechten waar ondersteund en wijzigingsdatums van gekopieerde bestanden worden verwerkt. ACL’s, uitgebreide attributen, eigenaarschap, sparse-opslag en hardlinkrelaties worden niet gerepliceerd. Er worden geen media geopend, geanalyseerd of gehercodeerd. De bestandssystemen moeten de gebruikte Linux-bestandsoperaties ondersteunen; een niet ondersteunde operatie stopt de uitvoering.

## Ontwikkeling

```bash
python3 -m unittest discover -s tests -v
python3 tools/preview.py
python3 tools/build_wheel.py
```

De tests werken uitsluitend met tijdelijke bestanden; de preview gebruikt fictieve mediabestanden en schrijft `build/syncshallot-preview.png`. Er is nog geen praktijktest op een echte NAS of USB-schijf uitgevoerd.

De paneelindeling is geïnspireerd op [Double Commander](https://doublecmd.github.io/doc/en/syncdirs.html) en [Total Commander](https://www.ghisler.com/screenshots/en/08.html). Het nieuwe icoon is met de ingebouwde imagegen-tool gemaakt met PixelPickle en SoundSausage als stijlreferentie; de gebruikte prompt staat in `syncshallot/assets/icon-prompt.txt`.
