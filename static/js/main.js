document.addEventListener('DOMContentLoaded', function () {
    const betSlipLegs = [];
    const betSlipList = document.getElementById('bet-slip-legs');
    const emptySlipMessage = document.getElementById('bet-slip-empty');
    const betSlipFooter = document.getElementById('bet-slip-footer');
    const betsJsonInput = document.getElementById('bets-json-input');
    const betSlipForm = document.getElementById('bet-slip-form');

    const oddsButtons = document.querySelectorAll('.js-odds-pick');

    function renderBetSlip() {
        // Clear current slip
        betSlipList.innerHTML = '';

        if (betSlipLegs.length === 0) {
            betSlipList.appendChild(emptySlipMessage);
            betSlipFooter.style.display = 'none';
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

    // Initial render
    renderBetSlip();
});