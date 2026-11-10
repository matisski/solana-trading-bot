# Bot do memecoinów na Solanie

Ten bot sam szuka popularnych tokenów na Solanie, odrzuca podejrzane i kupuje małe pozycje za USDC. Potem sam sprzedaje, gdy cena urośnie albo spadnie.

Nie łączy się z aplikacją fomo. Nie używa dźwigni. Nie kupuje po samej nazwie tokenu. Zawsze bierze dokładny adres (mint).

## Zanim cokolwiek wkleisz

Gdy w GitHubie jest klucz portfela, bot wydaje prawdziwe pieniądze.

Klucz wklejasz tylko w ustawieniach GitHuba. Nigdy w czacie, mailu, notatce ani w pliku tego projektu. Kto ma klucz, ma portfel.

## 1. Osobny portfel w Phantom na iPhonie

Nie używaj portfela, na którym trzymasz swoje główne pieniądze.

1. Otwórz aplikację Phantom.
2. Stuknij ikonę portfela u góry.
3. Dodaj nowy portfel i utwórz go. Nazwij go na przykład `Bot`.
4. Przełącz się na ten portfel, żeby był aktywny.

## 2. Pieniądze na tym portfelu

Bot kupuje za **USDC**, nie za SOL.

- USDC to zwykłe USDC z Phantom, sieć Solana. Jedno kupno kosztuje 5 USDC. Bot trzyma najwyżej 3 pozycje naraz, więc sensownie mieć około 15 USDC.
- Adres tego USDC jest taki: `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`. Nie zmieniaj go w pliku `desk.yaml`.
- SOL jest tylko na opłaty sieci. Wpłać około **0,02–0,03 SOL**. Tyle wystarcza na kilka transakcji i na otwarcie nowych kont tokenów.
- Jeśli SOL spadnie poniżej 0,005, bot nic nie wyśle. W raporcie napisze wprost, że SOL jest za mało. Wtedy doładuj SOL.

Jeśli bot pisze, że USDC jest 0, a w Phantom widzisz USDC, sprawdź czy to sieć Solana i czy to zwykłe USDC, a nie inny token o podobnej nazwie.

## 3. Klucz prywatny tylko do GitHuba

Potrzebny jest klucz prywatny tego jednego portfela bota. To długi ciąg liter i cyfr. To nie jest lista 12 słów.

1. W Phantom przełącz się na portfel bota.
2. Otwórz Ustawienia (zębatka).
3. Wejdź w bezpieczeństwo.
4. Wybierz eksport klucza prywatnego (Private Key). Nie wybieraj frazy odzyskiwania.
5. Potwierdź Face ID albo kod i skopiuj klucz.
6. Na iPhonie otwórz Safari i wejdź w:

   https://github.com/matisski/solana-trading-bot/settings/secrets/actions

7. Stuknij **New repository secret**.
8. W polu Name wpisz dokładnie: `SOLANA_PRIVATE_KEY`
9. W polu Value wklej klucz i zapisz.
10. Skopiuj do schowka jakieś zwykłe słowo, żeby klucz nie został w schowku. Nie wysyłaj klucza nikomu.

Opcjonalnie, gdyby raport często pisał o błędzie połączenia z Solaną, możesz dodać drugi sekret o nazwie `RPC_URL`. W wartość wklej adres RPC od dostawcy, nie klucz prywatny. Bez tego bota też da się uruchomić.

## 4. Jak uruchomić ręcznie

Bot pojawia się w zakładce Actions dopiero wtedy, gdy ten kod jest na gałęzi `main`. Jeśli właśnie dodajesz bota, najpierw otwórz pull request i stuknij **Merge**.

Potem:

1. Otwórz https://github.com/matisski/solana-trading-bot/actions
2. Stuknij **Solana Memecoin Bot**.
3. Stuknij **Run workflow**, a potem zielony przycisk.
4. Odśwież po minucie i otwórz to uruchomienie. Na górze jest podsumowanie.

Bot sam startuje mniej więcej co 5 minut. GitHub często spóźnia ten zegar. Czasem minie 10, 20 albo więcej minut.

Gdy projekt jest publiczny, jedno uruchomienie trwa około 4,5 minuty. W tym czasie bot sprawdza ceny otwartych pozycji co około 30 sekund. Nowych tokenów szuka najwyżej co półtorej minuty, żeby nie zatykać darmowych limitów stron. Potem się wyłącza, żeby nie zderzyć się z następnym uruchomieniem. Limity (3 pozycje, 10 USD straty na dzień, jedno nowe kupno na szukanie) liczą się przez cały ten czas.

Ten projekt jest teraz prywatny. Wtedy bot sprawdza ceny tylko raz na uruchomienie. Pętla co 30 sekund na prywatnym repozytorium zjadłaby darmowe minuty GitHuba w około półtora dnia. Gdy projekt stanie się publiczny, częstsze sprawdzanie włączy się samo.

Stop-loss nie jest natychmiastowy. Między sprawdzeniami mija około 30 sekund, a sam GitHub potrafi obudzić bota dużo później.

## 5. Jak czytać wyniki

Najprościej otwórz plik `state/ostatni_raport.md` w tym projekcie. Jest po polsku.

- `state/trades.csv` to historia kupien i sprzedaży. Przy prawdziwej transakcji jest podpis i link do Solscan.
- `state/state.json` to otwarte pozycje, szczyt ceny i wynik dnia.
- To samo podsumowanie jest na górze uruchomienia w zakładce Actions.

Link z Solscan można otworzyć w Safari i zobaczyć transakcję.

## 6. Prawdziwy handel i tryb próbny

Domyślnie bot handluje naprawdę, gdy sekret `SOLANA_PRIVATE_KEY` jest zapisany, a na portfelu są USDC i trochę SOL.

Tryb próbny nic nie wysyła. Włączysz go na jeden z dwóch sposobów:

- usuń sekret `SOLANA_PRIVATE_KEY`, albo
- dodaj zmienną (to nie jest sekret). Wejdź w:

  https://github.com/matisski/solana-trading-bot/settings/variables/actions

  Stuknij **New repository variable**. Name: `DRY_RUN`. Value: `true`.

W trybie próbnym bot i tak szuka tokenów, sprawdza je i zapisuje udawane kupna, żeby było widać, co by zrobił.

Żeby wrócić do prawdziwych transakcji: usuń zmienną `DRY_RUN` albo ustaw ją na `false`. Sekret z kluczem musi być na miejscu.

Pozycje zapisane w trybie próbnym nie są sprzedawane naprawdę.

Jak zatrzymać bota: ustaw `DRY_RUN` na `true` albo w Actions otwórz **Solana Memecoin Bot** i wybierz **Disable workflow**.

## 7. Co bot odrzuca

- token młodszy niż 45 minut,
- płynność poniżej 50 000 USD albo mały obrót,
- token, który w ostatniej godzinie urósł już o ponad 100%, albo w ostatnich 5 minutach spada,
- dużo więcej sprzedaży niż kupna,
- słaby wynik RugCheck,
- token, przy którym ktoś nadal może dodrukować monety albo zamrozić portfele,
- jeden portfel z za dużym udziałem,
- płynność, która nie jest zablokowana albo spalona,
- token, którego Jupiter nie umie od razu sprzedać z powrotem na USDC, gdy sam ten obrót zjada więcej niż około 3%, albo rusza cenę o więcej niż 1,5%.

## 8. Liczby, które możesz zmienić

Są w pliku `desk.yaml`. Domyślnie:

- jedno kupno: **5 USDC**
- najwyżej **3** otwarte pozycje
- na jedno szukanie najwyżej **1** nowe kupno, żeby nie wydać całych 15 USDC naraz. Przy pełnej pętli szukanie jest około co 90 sekund, nie co 30
- dzienny limit straty: **10 USD** (liczy się zrealizowana strata, dzień od północy UTC). Po limicie bot nie kupuje do następnego dnia, ale dalej pilnuje otwartych pozycji
- przy **2x** sprzedaje połowę
- twardy stop: **−8%** od ceny, którą dałoby się dostać zaraz po kupnie (nie od samego kosztu wejścia, bo sam obrót już trochę zjada). Jak cena skoczy między sprawdzeniami, strata może być większa niż 8%
- trailing stop: **10%** od najwyższej ceny, dopiero gdy cena była już wyżej niż przy kupnie
- minimalny wiek pary: **45 minut**
- minimalna płynność: **50 000 USD**, minimalny obrót godzinowy: **30 000 USD**
- poślizg: **3%**
- maksymalny wpływ na cenę przy kupnie i przy sprzedaży: **1,5%**
- test sprzedaży przed kupnem musi oddać co najmniej **97%** USDC
- minimum SOL, żeby w ogóle wysłać transakcję: **0,005**

Memecoiny są bardzo ryzykowne. Te limity nie dają gwarancji zysku. Możesz stracić wpłacone USDC.
