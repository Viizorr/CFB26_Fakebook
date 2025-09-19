document.addEventListener('DOMContentLoaded', function () {
    // Detect if we are on the game detail page by checking for the unique form ID
    const detailsSlipForm = document.getElementById('details-slip-form');

    if (detailsSlipForm) {
        /***************************************************
         * LOGIC FOR THE GAME DETAIL PAGE STATIC BET SLIP
         ***************************************************/
        const betSlip = []; // Will only ever hold 0 or 1 bet
        const oddsButtons = document.querySelectorAll('.js-odds-pick');
        const slipLegsList = document.getElementById('details-slip-legs');
        const slipEmptyMsg = document.getElementById('details-slip-empty');
        const slipFooter = document.getElementById('details-slip-footer');
        const stakeInput = document.getElementById('details-stake-input');
        const betsJsonInput = document.getElementById('details-bets-json-input');
        const riskAmountEl = document.getElementById('details-risk-amount');
        const payoutAmountEl = document.getElementById('details-payout-amount');

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

        function renderDetailsSlip() {
            // Clear list
            slipLegsList.innerHTML = '';
            
            if (betSlip.length === 0) {
                slipLegsList.appendChild(slipEmptyMsg);
                slipFooter.style.display = 'none';
                stakeInput.value = '';
            } else {
                const leg = betSlip[0];
                const li = document.createElement('li');
                li.className = 'list-group-item d-flex justify-content-between align-items-center';
                li.innerHTML = `
                    <span>${leg.label} <strong>(${leg.price})</strong></span>
                    <button class="btn-close" data-remove-id="${leg.betId}"></button>
                `;
                slipLeg