document.addEventListener('DOMContentLoaded', function () {
    // --- Global Bet Slip (Slide-up) Logic ---
    const betSlipLegs = [];
    const betSlipList = document.getElementById('bet-slip-legs'); // This should be for the global slip
    const emptySlipMessage = document.getElementById('bet-slip-empty');
    const betSlipFooter = document.getElementById('bet-slip-footer');
    const betsJsonInput = document.getElementById('bets-json-input');
    const betSlipForm = document.getElementById('bet-slip-form'); // Global form
    const stakeInput = document.getElementById('stake'); // Global stake input
    const riskAmountEl = document.getElementById('risk-amount'); // Global risk
    const payoutAmountEl = document.getElementById('payout-amount'); // Global payout

    const oddsButtons = document.querySelectorAll('.js-odds-pick');

    function calculateAmericanPayout(stake, price) {
        const stakeF = parseFloat(stake);
        const priceI = parseInt(price, 10);
        if (isNaN(stakeF) || stakeF <= 0) {
            return 0.00;
        }

        let profit = 0;
        if (priceI > 0) {
            profit = stakeF * (priceI / 100);
        } else {
            profit = stakeF * (100 / Math.abs(priceI));
        }
        return stakeF + profit;
    }

    function updateGlobalCalculation() {
        const stake = parseFloat(stakeInput.value) || 0;
        let payout = 0;

        if (betSlipLegs.length === 1) { // Single bet logic
            const price = betSlipLegs[0].price;
            payout = calculateAmericanPayout(stake, price);
        } else if (betSlipLegs.length > 1) { // Parlay logic
            let totalOdds = 1;
            for (const leg of betSlipLegs) {
                const price = parseInt(leg.price, 10);
                if (price > 0) {
                    totalOdds *= (price / 100) + 1;
                } else {
                    totalOdds *= (100 / Math.abs(price)) + 1;
                }
            }
            payout = stake * totalOdds;
        }

        riskAmountEl.textContent = `$${stake.toFixed(2)}`;
        payoutAmountEl.textContent = `$${payout.toFixed(2)}`;
        
        // Update form action based on single or parlay
        if (betSlipLegs.length > 1) {
            betSlipForm.querySelector('button[type="submit"]').textContent = 'Submit Parlay';
        } else {
            betSlipForm.querySelector('button[type="submit"]').textContent = 'Place Bet';
        }
    }


    function renderBetSlip() {
        if (!betSlipList || !emptySlipMessage || !betSlipFooter || !betsJsonInput) {
            console.error("Bet slip elements not found. Global bet slip may be missing from base.html.");
            return; // Gracefully exit if elements aren't present
        }

        // Clear current slip
        betSlipList.innerHTML = '';

        if (betSlipLegs.length === 0) {
            betSlipList.appendChild(emptySlipMessage);
            betSlipFooter.style.display = 'none';
            stakeInput.value = ''; // Clear stake when slip is empty
        } else {
            betSlipLegs.forEach(leg => {
                const li = document.createElement('li');
                li.className = 'list-group-item d-flex justify-content-between align-items-center';
                li.innerHTML = `
                    <span>${leg.label} <strong>(${leg.price})</strong></span>
                    <button class="btn-close" data-remove-id="${leg.betId}"></button>
                `;
                betSlipList.appendChild(li);
            });
            betSlipFooter.style.display = 'block';
        }

        // Update hidden input for form submission
        betsJsonInput.value = JSON.stringify(betSlipLegs.map(leg => ({
            gameId: leg.gameId,
            propId: leg.propId,
            betType: leg.betType,
            selection: leg.selection,
            price: leg.price,
            line: leg.line,
        })));

        // Always update calculation when slip changes
        updateGlobalCalculation();
    }

    oddsButtons.forEach(button => {
        button.addEventListener('click', function () {
            const betId = this.dataset.betId;
            const existingIndex = betSlipLegs.findIndex(leg => leg.betId === betId);

            if (existingIndex > -1) {
                // Bet already in slip, remove it
                betSlipLegs.splice(existingIndex, 1);
                this.classList.remove('active');
            } else {
                // Add new bet to slip
                betSlipLegs.push({
                    betId: betId,
                    gameId: this.dataset.gameId,
                    propId: this.dataset.propId || null,
                    betType: this.dataset.betType,
                    selection: this.dataset.selection,
                    label: this.dataset.label,
                    price: this.dataset.price,
                    line: this.dataset.line || null,
                });
                this.classList.add('active');
            }
            renderBetSlip();
        });
    });

    if (betSlipList) { // Check if betSlipList exists before adding listener
        betSlipList.addEventListener('click', function(e) {
            if (e.target.matches('[data-remove-id]')) {
                const betIdToRemove = e.target.dataset.removeId;
                const indexToRemove = betSlipLegs.findIndex(leg => leg.betId === betIdToRemove);
                if (indexToRemove > -1) {
                    betSlipLegs.splice(indexToRemove, 1);
                    // Deactivate the corresponding button
                    const buttonToDeactivate = document.querySelector(`[data-bet-id="${betIdToRemove}"]`);
                    if (buttonToDeactivate) {
                        buttonToDeactivate.classList.remove('active');
                    }
                    renderBetSlip();
                }
            }
        });
    }

    if (stakeInput) { // Add listener for stake input changes
        stakeInput.addEventListener('input', updateGlobalCalculation);
    }
    
    // Initial render
    renderBetSlip();
});