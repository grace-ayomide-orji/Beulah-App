function alertSuccess(titleContent, textContent){
    Swal.fire({
        icon: 'success',
        title: titleContent,
        text: textContent,
        confirmButtonText: 'Okay',      
        showConfirmButton: true,
        customClass: {
            title: 'my-swal-title',
            confirmButton: 'my-swal-confirm-btn',
        }
    });
}

function alertSuccess2(titleContent, textContent){
    Swal.fire({
        icon: 'success',
        title: titleContent,
        text: textContent,
        confirmButtonText: 'Okay',      
        showConfirmButton: true,
        timer: 1000,
        customClass: {
            title: 'my-swal-title',
            confirmButton: 'my-swal-confirm-btn',
        }
    });
}

function alertError(titleContent, textContent){
    Swal.fire({
        icon: 'error',
        title: titleContent,
        text: textContent,
        confirmButtonText: 'Okay',      
        showConfirmButton: true,
        customClass: {
            title: 'my-swal-title',
            confirmButton: 'my-swal-confirm-btn',
        }
    });
}

function confirmDelete(titleContent){
    return Swal.fire({
        title: titleContent,
        text: "You won't be able to revert this!",
        icon: "warning",
        showCancelButton: true,
        confirmButtonText: "Yes",
        cancelButtonText: "Cancel",
        reverseButtons: true,
        customClass: {
            title: 'my-swal-title',
            confirmButton: 'my-swal-confirm-btn',
            cancelButton: 'my-swal-cancel-btn',
            actions: 'my-swal-actions'
        }
    })
    .then(result => result.isConfirmed);// Returns true if "Yes" is clicked, false otherwise
}


document.addEventListener("DOMContentLoaded", function() {
    let preloader = document.getElementById("preloader");
    preloader.style.transition = "opacity 0.5s ease-out";
    preloader.style.opacity = "0";
    
    setTimeout(() => {
        preloader.style.display = "none";
        let mainContent = document.getElementById('content-container')
        mainContent.classList.add("loaded");
    }, 500);


    
    // Get the current URL path
    const currentPath = window.location.pathname;
    const activeNavKey = document.body.dataset.activeNav;

    // Select all navigation links
    const navLinks = document.querySelectorAll('.nav-link');

    // Loop through each link
    navLinks.forEach(link => {
      const linkKey = link.dataset.navKey;
      if ((activeNavKey && linkKey === activeNavKey) || (!activeNavKey && link.getAttribute('href') === currentPath)) {

        link.classList.add('active');
      } else {

        link.classList.remove('active');
      }
    });

    // Get the elements
    const burger = document.querySelector('.hamburger');
    const header = document.getElementById('header');
    const navHeader = document.getElementById('nav-header');
    const leftContainer = document.getElementById('left-container');
    const rightContainer = document.getElementById('right-container');
    const body = document.body;
    const fasIcons = document.querySelectorAll('.fas');
    const iconDescriptions = document.querySelectorAll('.icon-text-description');
    const mediaQuery = window.matchMedia("(max-width: 900px)"); 
    let isSidebarOpen; // Tracks current state
    let isToggled = false; // Tracks if the state has been toggled from default

    // Toggle sidebar function
    const toggleSidebar = () => {
        isSidebarOpen = !isSidebarOpen;
        isToggled = !isToggled; // Toggle the toggled state

        // Update sidebar classes
        if (isSidebarOpen) {
            navHeader.classList.remove("collapsed");
            leftContainer.classList.remove("collapsed");
            header.classList.remove("collapsed");
            rightContainer.classList.remove("collapsed");
            
            navHeader.classList.add("expanded");
            leftContainer.classList.add("expanded");
            header.classList.add("expanded");
            rightContainer.classList.add("expanded");
    
        } else {
            navHeader.classList.remove("expanded");
            leftContainer.classList.remove("expanded");
            header.classList.remove("expanded");
            rightContainer.classList.remove("expanded");

            navHeader.classList.add("collapsed");
            leftContainer.classList.add("collapsed");
            header.classList.add("collapsed");
            rightContainer.classList.add("collapsed");
        }

        // Update icon classes
        iconDescriptions.forEach(icon => {
            icon.classList.toggle("expanded", isSidebarOpen);
            icon.classList.toggle("collapsed", !isSidebarOpen);
        });

        fasIcons.forEach(icon => {
            icon.classList.toggle("expanded", isSidebarOpen);
            icon.classList.toggle("collapsed", !isSidebarOpen);
        });

        // Update burger icon: arrow when toggled, three dashes when not
        if (isToggled) {
            burger.classList.add("open");
        } else {
            burger.classList.remove("open");
        }
    };

    // Set initial state based on screen size
    const setInitialSidebarState = () => {
    if (mediaQuery.matches) {
        // Small screens: collapsed by default
        isSidebarOpen = false;
    } else {
        // Large screens: expanded by default
        isSidebarOpen = true;
    }
    isToggled = false; // Reset to default state

    // Set sidebar classes
    if (isSidebarOpen) {
        navHeader.classList.remove("collapsed");
        leftContainer.classList.remove("collapsed");
        header.classList.remove("collapsed");
        rightContainer.classList.remove("collapsed");
        
        navHeader.classList.add("expanded");
        leftContainer.classList.add("expanded");
        header.classList.add("expanded");
        rightContainer.classList.add("expanded");
    } else {
        navHeader.classList.remove("expanded");
        leftContainer.classList.remove("expanded");
        header.classList.remove("expanded");
        rightContainer.classList.remove("expanded");

        navHeader.classList.add("collapsed");
        leftContainer.classList.add("collapsed");
        header.classList.add("collapsed");
        rightContainer.classList.add("collapsed");
    }

    // Set icon classes
    iconDescriptions.forEach(icon => {
        icon.classList.toggle("expanded", isSidebarOpen);
        icon.classList.toggle("collapsed", !isSidebarOpen);
    });

    fasIcons.forEach(icon => {
        icon.classList.toggle("expanded", isSidebarOpen);
        icon.classList.toggle("collapsed", !isSidebarOpen);
    });

    // Set burger icon to three dashes (default state)
    burger.classList.remove("open");
    };


    // Event Listener for Burger Button
    burger.addEventListener("click", toggleSidebar);

    // Handle screen resize
    const handleResize = () => {
        setInitialSidebarState(); // Reset state on resize
    };

    // Set initial state on load
    setInitialSidebarState();

    // Listen for media query changes
    mediaQuery.addEventListener('change', handleResize);


   
    const year = document.getElementById('year')
    year.textContent = new Date().getFullYear()


  
})

// Timeout Fallback: Auto-hide loader after max 1 min
setTimeout(() => {
    let preloader = document.getElementById("preloader");
    let mainContent = document.getElementById("content-container");

    preloader.style.opacity = "0";
    setTimeout(() => {
        preloader.style.display = "none";
        mainContent.style.display = "block";
    }, 500);
}, 10000);

